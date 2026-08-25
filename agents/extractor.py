"""
Agent 4: ExtractorAgent — 清洗专家

职责: 对 FetcherRouter 产出的原始 HTML 进行清洗:
  - 去头（Header）、去尾（Footer）、去侧边栏（Sidebar）
  - 只保留正文内容和对应图片
  - 列表页二次拦截: 误抓的列表页在此丢弃

提取策略（三级降级）:
  1. trafilatura (主) — 快速、准确的正文抽取
  2. BeautifulSoup 启发式清洗 (降级) — 复用 nodes.py 清洗函数
  3. LLM 深度清洗 (深降级) — trafilatura + BS4 均失败时
"""

from __future__ import annotations

import re
import hashlib
import asyncio
from typing import Optional, List
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from bs4.element import Comment

from .models import SiteProfile, PageData
from .interfaces import ExtractorAgent as ExtractorAgentInterface

from schemas import agent_logger
import config


# ============================================================================
# trafilatura 集成（懒加载，允许模块未安装时降级）
# ============================================================================

_TRAFILATURA_AVAILABLE: Optional[bool] = None


def _check_trafilatura() -> bool:
    global _TRAFILATURA_AVAILABLE
    if _TRAFILATURA_AVAILABLE is None:
        try:
            import trafilatura  # noqa: F401
            _TRAFILATURA_AVAILABLE = True
        except ImportError:
            _TRAFILATURA_AVAILABLE = False
            agent_logger.warning(
                "[ExtractorAgent] trafilatura 未安装，将使用 BS4 启发式清洗。"
                "安装: pip install trafilatura"
            )
    return _TRAFILATURA_AVAILABLE


# ============================================================================
# LLM Fallback 客户端（懒加载单例）
# ============================================================================

_LLM_CLIENT = None


def _get_llm_client():
    """获取 LLM 客户端（用于深降级清洗）"""
    global _LLM_CLIENT
    if _LLM_CLIENT is None:
        from langchain_openai import ChatOpenAI
        _LLM_CLIENT = ChatOpenAI(
            model=config.get_model_name(),
            openai_api_key=config.DEEPSEEK_API_KEY,
            openai_api_base=config.DEEPSEEK_BASE_URL,
            temperature=0,
            max_tokens=4096,
            request_timeout=120,
            http_client=httpx.Client(
                timeout=httpx.Timeout(120.0, connect=15.0)
            ),
        )
    return _LLM_CLIENT


def reset_llm_client():
    """强制重置 LLM 客户端，使下次调用使用最新的 config 值"""
    global _LLM_CLIENT
    _LLM_CLIENT = None


# ============================================================================
# 列表页启发式检测
# ============================================================================

def _is_list_page(html: str, text_content: str) -> tuple[bool, float, str]:
    """
    检测页面是否为列表页（应在 NavAgent 阶段就已过滤，此处为二次拦截）。
    
    返回: (is_list, confidence, reason)
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return True, 1.0, "HTML 解析失败"

    text_len = len(text_content.strip())
    if text_len < 100:
        # 清洗后文本太短，用原始 HTML body 文本长度兜底
        body_text = soup.find("body")
        text_len = len(body_text.get_text(" ", strip=True)) if body_text else text_len

    # ★ 只统计「正文区」链接数：详情页模板的顶部导航 + 页脚 + 侧栏有大量链接
    #   （企业站常有 40~80 个），直接除以全文长度会让链接密度虚高，
    #   把详情页误判成列表页（从而跳过 LLM 正文定位）。排除导航容器后再算密度。
    _NAV_AREA_CLS = re.compile(
        r'(header|footer|nav|menu|topbar|top-bar|top_bar|breadcrumb|pagination|'
        r'aside|sidebar|head|foot|toolbar|tool-bar|service|float|bottom|'
        r'copyright|friendlink|link-box|links\b)', re.I
    )
    internal_links = 0
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        # 跳过位于头部/页脚/导航/侧栏容器内的链接（正邦等模板导航是 <div class="h_nav">，
        # 标签类型不定，需遍历全部祖先并检查 class/id）
        in_nav_area = False
        for p in a.find_parents():
            cls = " ".join(p.get("class") or [])
            cid = p.get("id") or ""
            if _NAV_AREA_CLS.search(cls) or _NAV_AREA_CLS.search(cid):
                in_nav_area = True
                break
        if in_nav_area:
            continue
        if href and not href.startswith(("http://", "https://", "//")):
            internal_links += 1
        elif href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
            internal_links += 1

    # 链接密度 = 正文区链接数 / 文本字符数
    link_density = internal_links / max(text_len, 1)

    # 判定规则
    reasons = []

    # 规则 1: 链接密度过高 (> 0.06，即每 100 个字符 6 个链接)
    if link_density > 0.06:
        reasons.append(f"链接密度过高 ({link_density:.4f})")

    # 规则 2: 正文过短但链接很多
    if text_len < 300 and internal_links > 10:
        reasons.append(f"正文过短({text_len}字)且链接过多({internal_links})")

    # 规则 3: 典型列表页 URL 特征
    url_lower = ""
    title_tag = soup.find("title")
    if title_tag:
        url_lower = title_tag.get_text("", strip=True).lower()
    list_keywords = ["列表", "list", "目录", "category", "归档", "archive", "search", "搜索结果"]
    for kw in list_keywords:
        if kw in url_lower:
            reasons.append(f"标题含列表关键词 '{kw}'")
            break

    # 规则 4: 有分页控件（覆盖常见 CMS 分页 class：pagination/pager/page-status/page-numbar 等）
    _pagination_cls = re.compile(
        r"(pagination|pager|page[-_]?(status|index|pre|next|last|num|bar|nav|navigation|numbar|number)|fenye|feny|turnpage)",
        re.I,
    )
    if soup.find_all(class_=_pagination_cls):
        reasons.append("检测到分页控件")

    # 规则 5: 分页文本特征（共N条 / 共N页 / 当前x/y页 / 第x/y页）
    _pager_text = re.compile(
        r'(共\s*\d+\s*条|共\s*\d+\s*页|当前\s*\d+\s*/\s*\d+\s*页|第\s*\d+\s*/\s*\d+\s*页)'
    )
    if _pager_text.search(soup.get_text(" ", strip=True)):
        reasons.append("检测到分页文本(共N条/当前x/y页)")

    if not reasons:
        return False, 0.0, ""

    confidence = min(0.5 + len(reasons) * 0.15, 1.0)
    return True, confidence, "; ".join(reasons)


# ============================================================================
# 正文提取核心函数
# ============================================================================

def _extract_with_trafilatura(html: str, url: str = "") -> tuple[str, str, float]:
    """
    使用 trafilatura 提取正文 (同步，在线程池中执行)。

    返回: (cleaned_html, text_content, confidence)
    """
    import trafilatura

    # trafilatura 2.x API 兼容: 尝试 output_format 和 format 两种参数名
    try:
        result = trafilatura.extract(
            html,
            output_format="xml",
            include_images=True,
            include_formatting=True,
            include_links=False,
            url=url,
        )
    except (TypeError, ValueError):
        # 回退: 不传 output_format，用默认输出
        result = trafilatura.extract(
            html,
            include_images=True,
            include_formatting=True,
            include_links=False,
            url=url,
        )

    if not result:
        return "", "", 0.0

    # 计算置信度
    try:
        text = trafilatura.extract(html, output_format="txt", include_images=False)
    except (TypeError, ValueError):
        try:
            text = trafilatura.extract(html, output_format="text", include_images=False)
        except (TypeError, ValueError):
            text = trafilatura.extract(html, include_images=False)

    if not text:
        return "", "", 0.0

    text_len = len(text.strip())
    html_len = len(html)

    # 置信度: 基于提取文本与原始 HTML 的比例
    if text_len > 500:
        confidence = 0.9
    elif text_len > 200:
        confidence = 0.7
    elif text_len > 50:
        confidence = 0.4
    else:
        confidence = 0.1

    return result, text, confidence


def _extract_with_bs4(html: str, url: str = "") -> tuple[str, str, float, int]:
    """
    使用 BeautifulSoup 启发式清洗正文（同步）。

    复用 nodes.py 中的清洗函数。

    返回: (cleaned_html, text_content, confidence, images_count)
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return html, "", 0.0, 0

    # 图片保护：统计原始图片数
    body = soup.find("body")
    total_imgs = len(_safe_find_all(body, "img")) if body else 0

    # 阶段 1: 移除非正文噪音
    for tag in soup.find_all(["script", "style", "noscript", "iframe", "link", "meta"]):
        tag.decompose()

    # 阶段 2: 去头去尾
    _bs4_remove_header_footer(soup, url)

    # 阶段 3: 去侧边栏/弹窗
    _bs4_remove_sidebar_ads(soup)

    # 阶段 4: 修复图片 src 为绝对路径
    _bs4_fix_image_srcs(soup, url)

    # 阶段 5: 尝试定位正文容器
    content = _bs4_find_content(soup)

    # 提取文本和图片数
    text = content.get_text(" ", strip=True) if content else ""
    imgs = len(_safe_find_all(content, "img")) if content else 0

    # 置信度
    text_len = len(text)
    if text_len > 500:
        confidence = 0.85
    elif text_len > 200:
        confidence = 0.6
    elif text_len > 50:
        confidence = 0.3
    else:
        confidence = 0.05

    cleaned_html = str(content) if content else html
    return cleaned_html, text, confidence, imgs


def _extract_with_llm(html: str, url: str) -> str:
    """
    调用 LLM 清洗 HTML（深降级，同步）。

    直接取出 body 文本，发送给 LLM 请求以 Markdown 格式输出正文。
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["script", "style", "noscript", "nav", "footer", "header"]):
            tag.decompose()
        body = soup.find("body")
        text = body.get_text(" ", strip=True)[:6000] if body else html[:6000]
    except Exception:
        text = html[:6000]

    llm = _get_llm_client()
    prompt = (
        "你是一个网页内容清洗专家。以下是从一个网页中提取的文本，可能包含导航菜单、"
        "页脚信息、侧边栏等非正文内容。\n\n"
        "请只保留正文内容，去除所有导航、页脚、侧边栏、广告等非正文噪音。\n"
        "如果原文包含图片的 alt 文本，请保留。\n"
        "以 Markdown 格式输出清洗后的正文内容。不要添加任何解释性文字。\n\n"
        f"--- 原始页面文本 ---\n{text}\n--- 结束 ---"
    )

    try:
        response = llm.invoke(prompt)
        return response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        agent_logger.warning(f"[ExtractorAgent] LLM 清洗失败: {e}")
        return text


# ============================================================================
# BS4 辅助清洗函数（内联版，避免依赖 nodes.py 的复杂依赖）
# ============================================================================

def _bs4_remove_header_footer(soup: BeautifulSoup, page_url: str = "") -> None:
    """移除头部/底部区域 + 关键词黑名单清理"""

    # ====================================================================
    # Step 1: 按 HTML 注释边界整体删除顶部全站导航区域
    # 删除范围: <!-- top 开始 --> 到 <!-- top 结束 --> 之间的所有元素
    # ====================================================================
    body = soup.find("body")
    if body:
        contents = list(body.contents)
        start_idx = None
        end_idx = None
        for i, node in enumerate(contents):
            if isinstance(node, Comment):
                text = str(node).strip()
                if "top 开始" in text:
                    start_idx = i
                elif "top 结束" in text:
                    end_idx = i

        if start_idx is not None and end_idx is not None and start_idx < end_idx:
            removed_count = 0
            # 从后往前删，避免索引变化
            for i in range(end_idx - 1, start_idx, -1):
                node = contents[i]
                if isinstance(node, Tag):  # 只删 Tag 节点
                    tag_attrs = node.attrs or {}
                    agent_logger.debug(
                        f"[Heuristic] Step1-topnav: 删除 <{node.name}> "
                        f"class=\"{' '.join(tag_attrs.get('class', []) or [])[:40]}\""
                    )
                    node.decompose()
                    removed_count += 1
            if removed_count > 0:
                agent_logger.debug(f"[Heuristic] Step1-topnav: 删除顶部导航容器 {removed_count} 个")
        elif not (start_idx is not None and end_idx is not None):
            # 回退: 无注释标记时按选择器删除
            for el in soup.select("div.fixedNav, div.top"):
                tag_attrs = el.attrs or {}
                agent_logger.debug(
                    f"[Heuristic] Step1-fallback: 删除 <{el.name}> "
                    f"class=\"{' '.join(tag_attrs.get('class', []) or [])[:40]}\""
                )
                el.decompose()

    # ====================================================================
    # Step 2: 删除全站装饰图（备案图标/国旗切换/二维码/Logo 遗留）
    # 三层过滤（清洗效果优先，命中即删，宁可误删不留残留）：
    #   ① 文件名/路径正则：二维码/备案/国旗等关键词无条件删（正文区也不留）
    #   ② 框架区位置：导航/页脚/侧栏/版权/面包屑容器内的 img 全删
    #   ③ 父容器语义：img 父容器 class/id 命中二维码/装饰语义 → 删
    # 正文区数据图标（如 icon1.jpg）不含关键词且不在框架区 → 自然保留。
    # ====================================================================
    _DECO_IMG_FILENAME_RE = re.compile(
        r"(?i)(szicbok\.gif|^cn\.gif$|^en\.gif$|tubiao\.png|template/default/images/"
        r"|erweima|qrcode|qr[_-]?code|wx[_-]?code|contentwx|weixin|wechat|saoyisao|sao[_-]?yi[_-]?sao)"
    )
    # 框架区容器特征（导航/页脚/侧栏/版权/面包屑/轮播/分页等）
    _FRAME_ZONE_CLS_RE = re.compile(
        r"(?i)(^(nav|header|footer|top|bottom|menu|sidebar|side|breadcrumb|crumb|copyright|"
        r"foot|head|toolbar|fixednav|topnav|bottomnav|slide|carousel|swiper|pagination|"
        r"page[_-]?list|subnav|icon[_-]?list|share|sns|hot|recommend|related|rank|search)$)"
    )
    _FRAME_ZONE_KW = (
        "nav", "footer", "header", "breadcrumb", "crumb", "copyright", "sidebar",
        "menu", "topbar", "toolbar", "bottom", "fixednav", "版权", "页脚", "导航",
        "面包屑", "侧栏", "侧边",
    )
    for img in list(soup.find_all("img")):
        src = (img.get("src") or "") or ""
        alt = (img.get("alt") or "") or ""
        # ① 文件名/路径正则 → 无条件删（含正文区二维码，如 contentwx.png）
        if _DECO_IMG_FILENAME_RE.search(src) or _DECO_IMG_FILENAME_RE.search(alt):
            agent_logger.debug(f"[Heuristic] Step2-①: 删除二维码/装饰图 alt={alt[:20]} src={src[-40:]}")
            img.decompose()
            continue
        # ② 框架区位置 → 祖先容器命中框架特征即删
        in_frame = False
        for p in img.parents:
            if not hasattr(p, "get"):
                continue
            if getattr(p, "name", "") in ("nav", "header", "footer", "aside"):
                in_frame = True
                break
            p_cls = " ".join(p.get("class", []) or [])
            p_id = (p.get("id") or "") or ""
            p_all = (p_cls + " " + p_id).lower()
            if _FRAME_ZONE_CLS_RE.search(p_cls) or _FRAME_ZONE_CLS_RE.search(p_id):
                in_frame = True
                break
            if any(kw in p_all for kw in _FRAME_ZONE_KW):
                in_frame = True
                break
        if in_frame:
            agent_logger.debug(f"[Heuristic] Step2-②: 删除框架区装饰图 src={src[-40:]}")
            img.decompose()
            continue
        # ③ 父容器语义（二维码容器）
        parent = img.parent
        if parent and hasattr(parent, "get"):
            p_cls = " ".join(parent.get("class", []) or [])
            p_id = (parent.get("id") or "") or ""
            if any(kw in (p_cls + " " + p_id).lower()
                   for kw in ("qrcode", "erweima", "二维码", "扫码", "wechat", "weixin")):
                agent_logger.debug(f"[Heuristic] Step2-③: 删除二维码容器图 src={src[-40:]}")
                img.decompose()

    # ====================================================================
    # Step 3: 移除所有 HTML 注释节点 (Comment)
    # ====================================================================
    comment_count = 0
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()
        comment_count += 1
    if comment_count > 0:
        agent_logger.debug(f"[Heuristic] Step3: 移除 HTML 注释 {comment_count} 个")

    # ====================================================================
    # Step 4: 清理 BOM / 编码碎片 (U+FEFF) 和空白的 NavigableString 节点
    # ====================================================================
    cleaned_strings = 0
    if body:
        for s in list(body.find_all(string=True)):
            if not s or not s.strip():
                continue
            if "\ufeff" in s:
                new_s = s.replace("\ufeff", "")
                s.replace_with(new_s)
                cleaned_strings += 1
        if cleaned_strings > 0:
            agent_logger.debug(f"[Heuristic] Step4: 清理 BOM 碎片 {cleaned_strings} 处")

    # ── 原有逻辑: header/nav/footer 标签移除 ──
    for tag in soup.find_all(["header", "nav", "footer"]):
        tag.decompose()

    # ── 关键词黑名单：扫描所有 div/section，命中非正文特征词则删除 ──
    _NOISE_KEYWORDS = [
        "扫码", "二维码", "关注公众号", "扫一扫",
        "版权所有", "copyright", "备案号", "icp",
        "联系电话", "传真", "地址", "邮箱", "e-mail",
        "设为首页", "加入收藏", "站点地图",
    ]
    _footer_re = re.compile("|".join(_NOISE_KEYWORDS), re.I)

    # ★ 先标记正文候选区域（后续不会被误删）
    _main_zones = set()
    for el in soup.find_all(["article", "main"]):
        _main_zones.add(id(el))
    for el in soup.find_all(["div", "section", "span"],
                           class_=re.compile(r"(content|article|post|detail|main-text|entry)", re.I)):
        _main_zones.add(id(el))

    def _in_main_zone(el) -> bool:
        for p in el.parents:
            if id(p) in _main_zones:
                return True
        return False

    # 扫描 body 下所有 div/section/section-group
    for el in list(soup.find_all(["div", "section"])):
        if _in_main_zone(el):
            continue  # ★ 正文安全阀：主内容区内不过滤
        text = el.get_text(" ", strip=True)
        if len(text) < 30:
            continue  # 太短的元素不判断
        # 如果文本中匹配 >= 2 个噪声关键词 → 删除
        matches = len(_footer_re.findall(text))
        if matches >= 2:
            el.decompose()
            continue
        # 如果文本包含电话/传真模式且是附近标签的子节点
        if re.search(r'(电话|传真|手机)[：:\s]*[\d\-]{7,}', text):
            cls = " ".join(el.get("class", [])).lower()
            if any(kw in cls for kw in ("footer", "contact", "info", "bottom")):
                el.decompose()

    body = soup.find("body")
    if not body:
        return

    direct_children = [c for c in list(body.children) if isinstance(c, Tag)]
    if len(direct_children) < 3:
        return

    # 基于位置的清理
    for child in direct_children[:2]:
        if _in_main_zone(child):
            continue
        text_len = len(child.get_text(strip=True))
        links = len(_safe_find_all(child, "a"))
        if text_len < 200 and links > 2:
            child.decompose()

    for child in direct_children[-2:]:
        if _in_main_zone(child):
            continue
        text_len = len(child.get_text(strip=True))
        if text_len < 100:
            child.decompose()

    # ── 通用启发式清洗（无效链接修复+侧边栏+页脚+安全兜底） ──
    _clean_content_heuristic(soup, page_url)


def _clean_content_heuristic(soup: BeautifulSoup, page_url: str = "") -> None:
    """
    通用启发式清洗 — 4步策略（在 trafilatura/BS4 提取前执行）。

    Step 1: 全部链接转黑 — 全局 <a> → <span>（离线页面不需要可点击链接）
    Step 2: 侧边栏静默识别 — 打分制删除
    Step 3: 页脚块级清除 — body 后 50% 区域
    Step 4: 安全兜底 — 30% 文本密度保护
    """
    import re

    body = soup.find("body")
    if not body:
        return

    # ── 预计算全页总文本（用于安全兜底） ──
    total_text = len(body.get_text(" ", strip=True))
    if total_text < 100:
        return  # 页面内容太少，不冒险删除

    # ====================================================================
    # Step 1: 全部超链接 → 黑色普通文本
    #   离线页面不需要可点击链接，只保留文本内容和图片。
    #   例外: 若 <a> 仅包裹 <img>，去掉 <a> 保留 <img>
    # ====================================================================
    fixed_count = 0
    for a in list(body.find_all("a")):
        # 例外：<a> 仅包裹图片（如导航 Logo）→ 拆掉 <a>，保留 <img>
        children = [c for c in a.children if not (isinstance(c, str) and not c.strip())]
        if children and all(
            hasattr(c, "name") and c.name == "img" for c in children
        ):
            a.unwrap()
            fixed_count += 1
            continue

        span = soup.new_tag("span")
        # 保留原有文本内容和子元素
        for child in list(a.children):
            span.append(child)
        if not span.get_text(strip=True):
            span.string = a.get_text(strip=True) or ""
        # 设为黑色普通文本
        existing_style = a.get("style", "")
        span["style"] = (existing_style + ";color:#000;text-decoration:none;cursor:text").strip(";")
        a.replace_with(span)
        fixed_count += 1

    if fixed_count > 0:
        agent_logger.debug(f"[Heuristic] Step1: 全部链接转黑 {fixed_count} 个")

    # ====================================================================
    # Step 2: 侧边栏/导航树静默识别 — 打分制
    # ====================================================================
    def _should_remove_sidebar(el) -> tuple:
        """
        对候选容器打分，返回 (should_remove, score, reason)。

        打分规则:
          - 链接密度 > 0.5: +3 分
          - 链接密度 > 0.3: +2 分
          - 文本 < 200 字 且 节点 > 5: +2 分
          - 位于 body 首/尾位置: +1 分
          - class/id 含 nav/menu/sidebar/tree/list: +1 分
          - 门槛: >= 5 分 且 不含 30% 全页文本
        """
        text = el.get_text(" ", strip=True)
        text_len = len(text)
        links = len(el.find_all("a", href=True))
        ld = links / max(text_len, 1)
        el_attrs = el.attrs or {}
        el_class = " ".join(el_attrs.get("class", [])).lower()
        el_id = el_attrs.get("id", "").lower()

        score = 0
        reasons = []

        # 链接密度
        if ld > 0.5:
            score += 3
            reasons.append(f"ld>{ld:.2f}")
        elif ld > 0.3:
            score += 2
            reasons.append(f"ld>{ld:.2f}")

        # 短文本 + 多节点
        child_nodes = len(list(el.descendants))
        if text_len < 200 and child_nodes > 5:
            score += 2
            reasons.append(f"short({text_len})+dense({child_nodes})")

        # 位置特征
        direct_children = [c for c in body.children if hasattr(c, 'name')]
        if direct_children:
            if el is direct_children[0] or el is direct_children[-1]:
                score += 1
                reasons.append("edge_position")

        # 关键词辅助
        nav_kw = ["nav", "menu", "sidebar", "tree", "left", "right-panel", "widget"]
        if any(kw in el_class or kw in el_id for kw in nav_kw):
            score += 1
            reasons.append("nav_kw")

        return score >= 5, score, "+".join(reasons)

    sidebar_removed = 0
    for el in list(body.find_all(["div", "section", "nav", "ul", "aside"])):
        # 安全兜底：该元素包含 > 30% 全页文本 → 绝对不删
        el_text = len(el.get_text(" ", strip=True))
        if el_text > total_text * 0.3:
            continue
        # 最小节点数：< 3 个子节点不处理
        el_children = [c for c in el.children if hasattr(c, 'name')]
        if len(el_children) < 3:
            continue

        should_remove, score, reason = _should_remove_sidebar(el)
        if should_remove:
            el.decompose()
            sidebar_removed += 1
            el_attrs = el.attrs or {}
            el_cls = " ".join(el_attrs.get("class", []) or [])[:40]
            agent_logger.debug(
                f"[Heuristic] Step2: 删除侧边栏 <{el.name}> "
                f"class=\"{el_cls}\" "
                f"score={score} ({reason})"
            )

    if sidebar_removed > 0:
        agent_logger.debug(f"[Heuristic] Step2: 删除侧边栏容器 {sidebar_removed} 个")

    # ====================================================================
    # Step 3: 页脚噪声块级清除 — body 后 50% 区域
    # ====================================================================
    # 找到 body 中所有块级子元素
    body_children = [c for c in body.children if hasattr(c, 'name')]
    if len(body_children) >= 2:
        mid = len(body_children) // 2
        bottom_half = body_children[mid:]  # 后 50%
    else:
        bottom_half = []

    footer_removed = 0
    _prev_next_re = re.compile(r'(上一篇|下一篇|上一条|下一条|上一页|下一页)', re.I)
    _footer_kw_re = re.compile(r'(苏州工商|工业和信息化部|公安机关备案|营业执照)', re.I)

    for el in list(body.find_all(["div", "p", "span"])):
        # 仅在非正文区域（后 50%）操作
        in_bottom = any(el is c or (hasattr(c, 'descendants') and el in c.descendants) for c in bottom_half)
        if not in_bottom:
            continue

        # 安全兜底
        el_text = len(el.get_text(" ", strip=True))
        if el_text > total_text * 0.3:
            continue

        el_children = [c for c in el.children if hasattr(c, 'name')]
        if len(el_children) < 3 and el_text < 10:
            continue  # 太小的元素不判断

        text = el.get_text(" ", strip=True)

        # 特征1: 仅含 1-2 短文本行或图片 + 周围无长段落
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        has_long = any(len(l) > 200 for l in lines)
        if len(lines) <= 2 and not has_long:
            # 检查上下文：前后兄弟节点是否有长文本
            prev = el.find_previous_sibling()
            nxt = el.find_next_sibling()
            prev_long = prev and len(prev.get_text(strip=True)) > 200 if hasattr(prev, 'get_text') else False
            next_long = nxt and len(nxt.get_text(strip=True)) > 200 if hasattr(nxt, 'get_text') else False
            if not prev_long and not next_long:
                # 孤立块 → 删除
                el.decompose()
                footer_removed += 1
                el_attrs = el.attrs or {}
                el_cls = " ".join(el_attrs.get("class", []) or [])[:30]
                agent_logger.debug(
                    f"[Heuristic] Step3: 删除孤立页脚块 <{el.name}> "
                    f"class=\"{el_cls}\""
                )
                continue

        # 特征2: 上一篇/下一篇 模式（仅在非正文区域）
        if _prev_next_re.search(text) or _footer_kw_re.search(text):
            el.decompose()
            footer_removed += 1
            el_attrs = el.attrs or {}
            el_cls = " ".join(el_attrs.get("class", []) or [])[:30]
            agent_logger.debug(
                f"[Heuristic] Step3: 删除页脚模式块 <{el.name}> "
                f"class=\"{el_cls}\" text=\"{text[:40]}\""
            )

    if footer_removed > 0:
        agent_logger.debug(f"[Heuristic] Step3: 删除页脚块 {footer_removed} 个")


def _safe_find_all(el, *args, **kwargs):
    """防 NavigableString 崩溃的 find_all 包装器"""
    if hasattr(el, "find_all"):
        return el.find_all(*args, **kwargs)
    return []


def _bs4_remove_sidebar_ads(soup: BeautifulSoup) -> None:
    """移除侧边栏、广告、弹窗"""
    for tag_name in ["aside"]:
        for el in soup.find_all(tag_name):
            el.decompose()

    sidebar_patterns = [
        "sidebar", "side-bar", "side_bar", "widget-area",
        "left-panel", "right-panel",
    ]
    for kw in sidebar_patterns:
        for el in soup.find_all(class_=re.compile(kw, re.I)):
            if len(_safe_find_all(el, "img")) < 3:
                el.decompose()
        for el in soup.find_all(id=re.compile(kw, re.I)):
            if len(_safe_find_all(el, "img")) < 3:
                el.decompose()

    popup_patterns = ["modal", "popup", "pop-up", "dialog", "overlay", "lightbox", "tooltip"]
    for kw in popup_patterns:
        for el in soup.find_all(class_=re.compile(kw, re.I)):
            if len(_safe_find_all(el, "img")) < 3:
                el.decompose()

    ad_patterns = ["advertisement", "adsense", "banner-ad", "sponsor", "-ad-", "google-ad", "dfp-ad"]
    for kw in ad_patterns:
        for el in soup.find_all(class_=re.compile(kw, re.I)):
            el.decompose()
        for el in soup.find_all(id=re.compile(kw, re.I)):
            el.decompose()

    for el in soup.find_all(style=re.compile(r"position\s*:\s*(fixed|sticky)", re.I)):
        if len(el.find_all("img")) < 3:
            el.decompose()


def _bs4_fix_image_srcs(soup: BeautifulSoup, page_url: str) -> None:
    """将相对路径 img src / data-src 转为绝对 URL"""
    if not page_url:
        return
    for img in soup.find_all("img"):
        # 修正 src
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-url"):
            val = img.get(attr, "")
            if val and not val.startswith(("http://", "https://", "data:")):
                img[attr] = urljoin(page_url, val)
        # 修正 srcset
        srcset = img.get("srcset", "")
        if srcset:
            new_parts = []
            for part in srcset.split(","):
                part = part.strip()
                url_part = part.split(" ")[0]
                if url_part and not url_part.startswith(("http://", "https://", "data:")):
                    url_part = urljoin(page_url, url_part)
                    rest = part.split(" ", 1)
                    new_parts.append(f"{url_part} {rest[1]}" if len(rest) > 1 else url_part)
                else:
                    new_parts.append(part)
            img["srcset"] = ", ".join(new_parts)
    # 修正 <source> 标签 (picture 元素)
    for source in soup.find_all("source"):
        srcset = source.get("srcset", "")
        if srcset:
            new_parts = []
            for part in srcset.split(","):
                part = part.strip()
                url_part = part.split(" ")[0]
                if url_part and not url_part.startswith(("http://", "https://", "data:")):
                    url_part = urljoin(page_url, url_part)
                    rest = part.split(" ", 1)
                    new_parts.append(f"{url_part} {rest[1]}" if len(rest) > 1 else url_part)
                else:
                    new_parts.append(part)
            source["srcset"] = ", ".join(new_parts)


def _bs4_find_content(soup: BeautifulSoup) -> Tag:
    """
    启发式定位正文内容区域。
    优先级: <article> > <main> > 最长文本的 div > body
    """
    # 1. <article> 标签
    article = soup.find("article")
    if article and len(article.get_text(strip=True)) > 100:
        return article

    # 2. <main> 标签
    main = soup.find("main")
    if main and len(main.get_text(strip=True)) > 100:
        return main

    # 3. 查找 id/class 含 content/main/article 的容器
    content_patterns = [
        "content", "main-content", "article", "post", "entry",
        "detail", "news-detail", "news-content",
    ]
    for kw in content_patterns:
        for el in soup.find_all(["div", "section"], class_=re.compile(kw, re.I)):
            if len(el.get_text(strip=True)) > 200:
                return el
        for el in soup.find_all(["div", "section"], id=re.compile(kw, re.I)):
            if len(el.get_text(strip=True)) > 200:
                return el

    # 4. 找到文本最长的 div
    body = soup.find("body")
    if body:
        best_div = None
        best_len = 0
        for div in body.find_all("div", recursive=True):
            text_len = len(div.get_text(strip=True))
            if text_len > best_len:
                best_len = text_len
                best_div = div
        if best_div and best_len > 200:
            return best_div

    return body if body else soup


# ============================================================================
# ExtractorAgent 实现
# ============================================================================

class TrafilaturaExtractor(ExtractorAgentInterface):
    """
    基于 trafilatura + BS4 降级 + LLM 深降级的清洗器。

    清洗流程:
      1. trafilatura 提取 (confidence ≥ 0.6 → 通过)
      2. BS4 启发式清洗 (confidence < 0.6 时降级)
      3. LLM 深降级 (trafilatura + BS4 均 confidence < 0.3)
      4. 列表页二次拦截 (任意阶段)
    """

    def __init__(self) -> None:
        self._trafilatura_ok = _check_trafilatura()

    async def extract(self, page: PageData, profile: SiteProfile) -> PageData:
        """
        清洗 PageData.html，返回更新后的 PageData。
        """
        html = page.html
        url = page.url

        if not html or len(html) < 50:
            agent_logger.warning(f"[ExtractorAgent] 输入 HTML 过短 ({len(html)} 字节)，放弃清洗")
            page.content_quality_score = 0.0
            page.is_list_page_detected_at_extract = True
            return page

        loop = asyncio.get_running_loop()

        # ── 阶段 1: trafilatura ──
        if self._trafilatura_ok:
            try:
                cleaned, text, confidence = await loop.run_in_executor(
                    None, _extract_with_trafilatura, html, url
                )
            except Exception as e:
                agent_logger.warning(f"[ExtractorAgent] trafilatura 异常: {e}")
                cleaned, text, confidence = "", "", 0.0
        else:
            cleaned, text, confidence = "", "", 0.0

        # ── 阶段 2: BS4 降级 ──
        if confidence < 0.6:
            try:
                bs4_html, bs4_text, bs4_conf, img_count = await loop.run_in_executor(
                    None, _extract_with_bs4, html, url
                )
            except Exception as e:
                agent_logger.warning(f"[ExtractorAgent] BS4 清洗异常: {e}")
                bs4_html, bs4_text, bs4_conf, img_count = html, "", 0.0, 0
        else:
            bs4_html, bs4_text, bs4_conf, img_count = "", "", 0.0, 0

        # ── 阶段 3: 决策（选 trafilatura 或 BS4 结果） ──
        if confidence >= 0.6:
            final_html = cleaned
            final_confidence = confidence
            method = "trafilatura"
        elif bs4_conf >= 0.3:
            final_html = bs4_html
            final_confidence = bs4_conf
            method = "bs4"
        else:
            # ── 阶段 4: LLM 深降级（全自动分级） ──
            # 决策依据：
            #   1) 图集页/纯图片页（图多文少）→ 跳过 LLM（LLM 产不出正文，纯浪费）
            #   2) 正文页但规则提取失败（原始文本多、提取文本少）→ 值得 LLM 深降级补救
            #   3) 原始文本本身就少（空页/纯导航/极小页）→ 跳过 LLM（没有可补救的正文）
            img_cnt_raw = len(BeautifulSoup(html, "html.parser").find_all("img"))
            text_len_bs4 = len((bs4_text or "").strip())
            raw_text_len = len(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))

            if img_cnt_raw >= 3 and text_len_bs4 < 300:
                # 情况 1：图集页
                agent_logger.info(
                    f"[ExtractorAgent] 图集页跳过 LLM 深降级 "
                    f"(imgs={img_cnt_raw}, text={text_len_bs4}) | {url[:60]}"
                )
                final_html = bs4_html or cleaned or html
                final_confidence = max(bs4_conf, confidence, 0.3)
                method = "bs4"
            elif raw_text_len >= 300 and text_len_bs4 < 80:
                # 情况 2：正文页但提取失败（原始文本 A≥300，提取文本 B<80）
                agent_logger.info(
                    f"[ExtractorAgent] 正文页提取失败，触发 LLM 深降级 "
                    f"(raw_text={raw_text_len}, extracted={text_len_bs4}) | {url[:60]}"
                )
                try:
                    llm_text = await loop.run_in_executor(
                        None, _extract_with_llm, html, url
                    )
                    final_html = f"<div>{llm_text}</div>"
                    final_confidence = 0.5
                    method = "llm"
                except Exception as e:
                    agent_logger.warning(f"[ExtractorAgent] LLM 深降级也失败: {e}")
                    final_html = bs4_html or cleaned or html
                    final_confidence = max(confidence, bs4_conf, 0.1)
                    method = "fallback_failed"
            else:
                # 情况 3：原始文本本身就少，LLM 无可补救
                agent_logger.info(
                    f"[ExtractorAgent] 低文本页面跳过 LLM 深降级 "
                    f"(raw_text={raw_text_len}, extracted={text_len_bs4}) | {url[:60]}"
                )
                final_html = bs4_html or cleaned or html
                final_confidence = max(bs4_conf, confidence, 0.1)
                method = "bs4_auto"

        # ── 列表页二次拦截 ──
        # 用原始 HTML 的 body 文本做基础判定（不受清洗失败影响）
        text_for_check = await loop.run_in_executor(
            None, lambda: BeautifulSoup(html, "html.parser").find("body")
        )
        text_for_check = text_for_check.get_text(" ", strip=True) if text_for_check else ""
        is_list, list_conf, reason = _is_list_page(html, text_for_check)

        if is_list:
            agent_logger.info(f"[ExtractorAgent] 列表页二次拦截: {reason} (confidence={list_conf:.2f}) | {url[:80]}")
            page.is_list_page_detected_at_extract = True
            page.content_quality_score = 0.0
            page.html = ""
            return page

        # ── 更新 PageData ──
        page.html = final_html
        page.content_quality_score = final_confidence

        # 图片路径绝对化（相对路径 → 完整 http 路径）
        final_html = await loop.run_in_executor(
            None, _absolutize_image_urls, final_html, page.url
        )
        page.html = final_html

        # 收集图片
        img_cnt, img_urls, img_alts = await loop.run_in_executor(
            None, _collect_images, final_html
        )
        page.images_count = img_cnt
        page.images_urls = img_urls[:20]  # 最多保留 20 张图片
        page.images_alts = img_alts[:20]

        # 计算内容 MD5 指纹（用于去重）
        text_for_hash = await loop.run_in_executor(
            None, lambda: BeautifulSoup(final_html, "html.parser").get_text(" ", strip=True)
            if final_html else ""
        )
        page.content_hash = _compute_md5(text_for_hash)

        agent_logger.info(
            f"[ExtractorAgent] 清洗完成 | method={method} | "
            f"confidence={final_confidence:.2f} | html_len={len(final_html)} | "
            f"imgs={img_cnt} | hash={page.content_hash[:8]} | "
            f"url={url[:80]}"
        )
        return page


def _collect_images(html: str) -> tuple[int, List[str], List[str]]:
    """统计 HTML 中的图片数量、URL 和 Alt 文本"""
    urls: List[str] = []
    alts: List[str] = []
    try:
        soup = BeautifulSoup(html, "html.parser")

        # ── 1. 标准 <img> 标签 ──
        for img in soup.find_all("img"):
            src = (
                img.get("src") or
                img.get("data-src") or
                img.get("data-original") or
                img.get("data-lazy-src") or
                img.get("data-url") or
                ""
            ).strip()
            if src and not src.startswith("data:"):  # 过滤 base64 内嵌图，CSV 只留外链
                urls.append(src)
            alt = (img.get("alt") or img.get("title") or "").strip()
            alts.append(alt)

            # srcset 中第一个 URL 也收集
            srcset = img.get("srcset", "")
            if srcset:
                first = srcset.split(",")[0].strip().split(" ")[0]
                if first:
                    urls.append(first)

        # ── 2. trafilatura 输出的 <graphic> 标签 ──
        for g in soup.find_all("graphic"):
            src = g.get("src", "").strip()
            if src:
                urls.append(src)
            alt = g.get("alt", g.get("title", "")).strip()
            alts.append(alt)

        # ── 3. <picture> / <source> 标签 ──
        for pic in soup.find_all("picture"):
            for source in pic.find_all("source"):
                srcset = source.get("srcset", "")
                if srcset:
                    first = srcset.split(",")[0].strip().split(" ")[0]
                    if first:
                        urls.append(first)
            # fallback <img> inside <picture>
            fallback = pic.find("img")
            if fallback:
                src = (
                    fallback.get("src") or
                    fallback.get("data-src") or
                    fallback.get("data-original") or
                    ""
                ).strip()
                if src and src not in urls:
                    urls.append(src)

        return len(urls), urls, alts
    except Exception:
        return 0, [], []


def _compute_md5(text: str) -> str:
    """计算纯文本的 MD5 指纹"""
    if not text:
        return ""
    return hashlib.md5(text.strip().encode("utf-8", errors="replace")).hexdigest()


def _absolutize_image_urls(html: str, base_url: str) -> str:
    """将 HTML 中所有图片相关标签的相对路径转为绝对路径"""
    if not html:
        return html
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["img", "graphic", "source"]):
            # src 属性
            for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-url"):
                val = tag.get(attr, "")
                if val and not val.startswith(("http://", "https://", "data:")):
                    tag[attr] = urljoin(base_url, val)
            # srcset 属性
            srcset = tag.get("srcset", "")
            if srcset:
                new_parts = []
                for part in srcset.split(","):
                    part = part.strip()
                    url_part = part.split(" ")[0]
                    if url_part and not url_part.startswith(("http://", "https://", "data:")):
                        url_part = urljoin(base_url, url_part)
                        rest = part.split(" ", 1)
                        new_parts.append(f"{url_part} {rest[1]}" if len(rest) > 1 else url_part)
                    else:
                        new_parts.append(part)
                tag["srcset"] = ", ".join(new_parts)
        return str(soup)
    except Exception:
        return html
