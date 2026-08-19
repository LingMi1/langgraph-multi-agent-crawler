"""
graph/nodes.py — LangGraph 多 Agent 爬虫的全部节点函数

每个节点:
  - 接收 CrawlerState，返回部分状态更新（dict）
  - 复用 agents/ 下的现有 Agent 实例
  - 传统爬虫始终是默认执行者，LLM 仅在 evaluate_node 介入
"""

from __future__ import annotations

import os
import re
import uuid
import asyncio
import hashlib
from typing import Dict, Any, List, Optional, Set, Tuple
from datetime import datetime
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup, Comment

from .state import CrawlerState, EvaluationResult, QualityIssue, CrawlerConfig, ExtractionRules, GeneratedRule

# ── 全局配置 ──
MAX_RETRY_COUNT = 3  # 失败 URL 最大重试次数

# 复用现有 Agent 模块
from agents.scout import PageScout
from agents.nav import NavigationParser
from agents.fetcher import HttpxPlaywrightFetcher
from agents.extractor import (
    TrafilaturaExtractor,
    _is_list_page,
    _extract_with_trafilatura,
    _extract_with_bs4,
    _collect_images,
    _absolutize_image_urls,
    _compute_md5,
)
from agents.storage import FileSystemStorage, CSV_FIELDS
from agents.models import SiteProfile, NavLink, PageData, CrawlResult

from schemas import agent_logger
import config


# ============================================================================
# 全局 Agent 实例（模块级懒加载单例，在 LangGraph 节点间共享）
# ============================================================================

_scout: Optional[PageScout] = None
_nav: Optional[NavigationParser] = None
_fetcher: Optional[HttpxPlaywrightFetcher] = None
_extractor: Optional[TrafilaturaExtractor] = None
_storage: Optional[FileSystemStorage] = None
_llm_client: Optional[Any] = None


def _get_scout() -> PageScout:
    global _scout
    if _scout is None:
        _scout = PageScout()
    return _scout


def _get_nav() -> NavigationParser:
    global _nav
    if _nav is None:
        _nav = NavigationParser()
    return _nav


def _get_fetcher(crawler_config: dict = None) -> HttpxPlaywrightFetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = HttpxPlaywrightFetcher()
    if crawler_config:
        _fetcher.configure(
            request_delay=crawler_config.get("request_delay"),
            use_system_chrome=crawler_config.get("use_system_chrome", False),
        )
    return _fetcher


def _get_extractor() -> TrafilaturaExtractor:
    global _extractor
    if _extractor is None:
        _extractor = TrafilaturaExtractor()
    return _extractor


def _get_storage() -> FileSystemStorage:
    global _storage
    if _storage is None:
        _storage = FileSystemStorage()
    return _storage


def _get_llm():
    """获取 LLM 客户端（DeepSeek，用于评估节点）"""
    global _llm_client
    if _llm_client is None and config.DEEPSEEK_API_KEY:
        try:
            from langchain_openai import ChatOpenAI
            _llm_client = ChatOpenAI(
                model=config.get_model_name(),
                openai_api_key=config.DEEPSEEK_API_KEY,
                openai_api_base=config.DEEPSEEK_BASE_URL,
                temperature=0,
                max_tokens=1024,
                request_timeout=60,
                http_client=httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)),
            )
        except Exception as e:
            agent_logger.warning(f"[Graph] LLM 客户端初始化失败: {e}")
    return _llm_client


def reset_llm():
    """强制重置 LLM 客户端，使下次调用使用最新的 config 值"""
    global _llm_client
    _llm_client = None


# ============================================================================
# 辅助函数
# ============================================================================

def _url_key(url: str) -> str:
    """生成 URL 去重键（保留查询参数，过滤追踪参数）"""
    from urllib.parse import parse_qsl, urlencode
    parsed = urlparse(url)
    path = parsed.path.rstrip("/").lower()
    # 保留查询参数，但过滤追踪/会话参数
    if parsed.query:
        TRACKING_PARAMS = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term',
                           'utm_content', '_t', '_', 't', 'token', 'session', 'sid',
                           'random', '_dc', 'nocache', 'v', 'ver', 'timestamp', 'ts'}
        params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                  if k.lower() not in TRACKING_PARAMS]
        if params:
            query = urlencode(sorted(params))
            return f"{parsed.netloc}{path}?{query}".rstrip("/").lower()
    return f"{parsed.netloc}{path}".rstrip("/").lower()


def _extract_body_links(html: str, page_url: str, base_host: str) -> List[Tuple[str, str]]:
    """
    从页面 body 中提取所有同域内部链接（复现 pipeline.py 的 BFS 逻辑）。
    Returns: [(abs_url, link_text), ...]
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body") or soup
    links: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    for a in body.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        low = href.lower()
        if any(low.startswith(s) for s in ("javascript:", "mailto:", "tel:", "#")):
            continue
        abs_url = urljoin(page_url, href)
        parsed = urlparse(abs_url)
        if parsed.netloc.lower() != base_host:
            continue
        path = parsed.path.lower()
        if path and any(
            path.endswith(ext)
            for ext in (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
                        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip",
                        ".rar", ".mp4", ".mp3", ".css", ".js", ".ico")
        ):
            continue
        key = _url_key(abs_url)
        if key not in seen:
            seen.add(key)
            text = a.get_text(strip=True)[:50]
            links.append((abs_url, text))
    return links


def _extract_pagination_links(html: str, page_url: str, base_host: str) -> List[Tuple[str, str]]:
    """
    从 JS 驱动的分页组件中提取所有分页 URL。
    
    支持两种常见的 CMS 分页模式:
      1. TRS/政府 CMS: <a tagname="/path/uuid-2.html" onclick="queryArticleByCondition(...)">
         配合 <input name="article_paging_list_hidden" totalpage="17">
      2. 常见 CMS: <a href="index_2.html">2</a> 等 (已由 _extract_body_links 覆盖)
    
    Returns: [(abs_url, "分页-2"), ...]
    """
    import re as _re
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    links: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    # ── 模式1: tagname 属性承载 URL 的 JS 分页 ──
    # 先找 totalpage（TRS CMS 分页标志，静态 HTML 中始终存在）
    totalpage = 0
    for inp in soup.find_all("input", attrs={"totalpage": True}):
        try:
            totalpage = int(inp.get("totalpage", "0"))
        except ValueError:
            pass
        if totalpage > 1:
            break
    if totalpage <= 1:
        # 无 totalpage → 不是 TRS CMS 分页，跳过模式1
        pass
    else:
        for a in soup.find_all("a", attrs={"tagname": True}):
            tagname = a.get("tagname", "").strip()
            if not tagname:
                continue
            # tagname 格式: /jggs/xwjj/gsyw/8d466fbe-2.html
            # 不依赖 onclick（httpx 可能拿不到完整 JS 属性），直接用 tagname 解析
            m = _re.match(r'^(.+?)(\d+)(\.html?)$', tagname)
            if not m:
                continue
            path_prefix = m.group(1)
            page_suffix = m.group(3)
            # 生成所有分页 URL
            for p in range(1, totalpage + 1):
                rel_url = f"{path_prefix}{p}{page_suffix}"
                abs_url = urljoin(page_url, rel_url)
                if urlparse(abs_url).netloc.lower() != base_host:
                    continue
                key = _url_key(abs_url)
                if key not in seen:
                    seen.add(key)
                    links.append((abs_url, f"分页-{p}"))

    # ── 模式2: 数字分页链接 (href 为空但有 onclick) ──
    if not links:
        pagination_div = soup.find("div", class_=_re.compile(r'page|pag', re.I))
        if pagination_div:
            for a in pagination_div.find_all("a", onclick=True):
                onclick = a.get("onclick", "")
                # 尝试从 onclick 中提取 URL: goPage('index_2.html') / location.href='...'
                url_match = _re.search(r"""['"]([^'"]*\.html?[^'"]*)['"]""", onclick)
                if url_match:
                    rel_url = url_match.group(1).strip()
                    abs_url = urljoin(page_url, rel_url)
                    if urlparse(abs_url).netloc.lower() == base_host:
                        key = _url_key(abs_url)
                        if key not in seen:
                            seen.add(key)
                            links.append((abs_url, a.get_text(strip=True)[:20]))

    return links


# ============================================================================
# 图片抢救辅助 — 5层策略（必须在内容清洗之前执行）
# ============================================================================

_LAZY_ATTRS = [
    "data-src", "data-original", "data-lazy-src", "data-actualsrc",
    "data-url", "data-img", "data-pic", "data-bg", "data-background",
]

_PLACEHOLDER_KW = [
    "data:image", "1x1", "placeholder", "lazy", "spacer",
    "blank.gif", "blank.png", "loading.gif", "loading.svg",
    "transparent", "pixel.gif", "pixel.png", "empty.gif",
    "grey.gif", "ajax-loader", "grey-pixel", "white-pixel",
]


def _is_placeholder_src(src: str) -> bool:
    if not src:
        return True
    return any(kw in src.lower() for kw in _PLACEHOLDER_KW)


# ============================================================================
# 反爬拦截检测 — 识别高级反爬页面（Cloudflare/Datadome/Akamai 等）
# ============================================================================

_ANTI_CRAWL_SIGNATURES = [
    # Cloudflare
    "cloudflare", "turnstile", "just a moment", "challenge-platform",
    "cf-browser-verify", "__cf_chl", "cf-chl",
    # DataDome / Akamai / Incapsula / Imperva
    "datadome", "akamai", "incapsula", "imperva", "distil",
    # 通用拦截
    "access denied", "request blocked", "your request",
    "please enable javascript", "browser check",
    # 中文反爬提示
    "请完成安全验证", "正在进行人机验证", "滑块验证", "请稍后重试",
    "检测到异常访问", "请开启 JavaScript", "安全检测中",
    # 验证码
    "captcha", "recaptcha", "hcaptcha", "geetest",
]


def _detect_anti_crawl_block(html: str, fetch_method: str = "") -> tuple:
    """
    检测页面是否为反爬拦截页面（非正常内容）。

    Returns: (is_blocked: bool, reason: str)
    """
    import logging

    if fetch_method == "anti_crawl_blocked":
        logging.debug("[AntiCrawl Detail] 类型=Playwright超时, 已标记为高级反爬")
        return True, "高级反爬爬不了"

    if not html or len(html) < 100:
        return False, ""

    html_lower = html.lower()

    # 短响应 + 拦截关键词 → 强信号
    if len(html) < 1000:
        for sig in _ANTI_CRAWL_SIGNATURES:
            if sig in html_lower:
                logging.debug(f"[AntiCrawl Detail] 类型={sig}, 已标记为高级反爬")
                return True, "高级反爬爬不了"

    # 长响应中检测 Turnstile/验证码占位
    for sig in ["turnstile", "challenge-platform", "cf-browser-verify",
                "datadome", "geetest", "请完成安全验证"]:
        if sig in html_lower:
            logging.debug(f"[AntiCrawl Detail] 类型={sig}, 已标记为高级反爬")
            return True, "高级反爬爬不了"

    return False, ""


def _rescue_images(raw_html: str, page_url: str) -> tuple:
    """
    图片抢救 — 5层策略（复制自旧 LangGraph 模式 image_rescue_node）。

    Returns: (rescued_html, stats_dict)
    """
    import re

    stats = {"total": 0, "path_fixed": 0, "lazy_fixed": 0, "css_extracted": 0, "antihotlink": 0}

    if not raw_html or not page_url:
        return raw_html, stats

    try:
        soup = BeautifulSoup(raw_html, "html.parser")
    except Exception:
        return raw_html, stats

    # ===== 第1层：路径补全（相对/协议相对 → 绝对） =====
    for img in soup.find_all("img"):
        src = img.get("src", "").strip()
        if not src or src.startswith("data:"):
            continue
        stats["total"] += 1
        if src.startswith("//"):
            img["src"] = "https:" + src
            stats["path_fixed"] += 1
        elif not src.startswith(("http://", "https://")):
            try:
                img["src"] = urljoin(page_url, src)
                stats["path_fixed"] += 1
            except Exception:
                pass

    # ===== 第2层：懒加载破解 =====
    for img in soup.find_all("img"):
        src = img.get("src", "").strip()
        if _is_placeholder_src(src):
            for attr in _LAZY_ATTRS:
                val = img.get(attr, "").strip()
                if val and not val.startswith("data:") and not _is_placeholder_src(val):
                    if val.startswith("//"):
                        val = "https:" + val
                    elif not val.startswith(("http://", "https://")):
                        try:
                            val = urljoin(page_url, val)
                        except Exception:
                            continue
                    img["src"] = val
                    stats["lazy_fixed"] += 1
                    for a in _LAZY_ATTRS:
                        if img.has_attr(a):
                            del img[a]
                    break
        # 同时处理 src 缺失但懒加载属性有值的 img
        if not img.get("src", "").strip():
            for attr in _LAZY_ATTRS:
                val = img.get(attr, "").strip()
                if val and not val.startswith("data:"):
                    img["src"] = urljoin(page_url, val) if not val.startswith(("http", "//")) else val
                    stats["lazy_fixed"] += 1
                    stats["total"] += 1
                    for a in _LAZY_ATTRS:
                        if img.has_attr(a):
                            del img[a]
                    break

    # ===== 第3层：CSS 背景图提取 → <img> =====
    bg_re = re.compile(r'url\(["\']?(.*?)["\']?\)', re.I)
    for el in list(soup.find_all(style=True)):
        style_text = el.get("style", "")
        if "background" not in style_text.lower():
            continue
        for bg_url in bg_re.findall(style_text):
            bg_url = bg_url.strip()
            if not bg_url or bg_url.startswith("data:"):
                continue
            if bg_url.startswith("//"):
                abs_url = "https:" + bg_url
            elif not bg_url.startswith(("http://", "https://")):
                try:
                    abs_url = urljoin(page_url, bg_url)
                except Exception:
                    continue
            else:
                abs_url = bg_url
            existing = [i for i in el.find_all("img") if i.get("src") == abs_url]
            if existing:
                continue
            new_img = soup.new_tag("img", src=abs_url,
                                   style="max-width:100%;height:auto;display:block;")
            new_img["referrerpolicy"] = "no-referrer"
            el.insert(0, new_img)
            stats["css_extracted"] += 1
            stats["total"] += 1

    # Swiper 轮播图 data-background
    for div in soup.find_all(attrs={"data-background": True}):
        bg_url = div.get("data-background")
        if bg_url and not bg_url.startswith("data:"):
            div.append(soup.new_tag("img", src=urljoin(page_url, bg_url),
                                    **{"data-source": "swiper-bg"}))
            stats["css_extracted"] += 1
            stats["total"] += 1

    # ===== 第4层：防盗链绕过（meta referrer + img referrerpolicy） =====
    head = soup.find("head")
    if head:
        if not head.find("meta", attrs={"name": "referrer"}):
            meta_tag = soup.new_tag("meta")
            meta_tag["name"] = "referrer"
            meta_tag["content"] = "no-referrer"
            head.insert(0, meta_tag)
    else:
        html_tag = soup.find("html")
        if html_tag:
            head_tag = soup.new_tag("head")
            meta_tag = soup.new_tag("meta")
            meta_tag["name"] = "referrer"
            meta_tag["content"] = "no-referrer"
            head_tag.insert(0, meta_tag)
            html_tag.insert(0, head_tag)

    for img in soup.find_all("img"):
        if not img.get("referrerpolicy"):
            img["referrerpolicy"] = "no-referrer"
    stats["antihotlink"] = len(soup.find_all("img"))

    return str(soup), stats


def _merge_content_images(rescued_html: str, cleaned_html: str, page_url: str = "") -> tuple:
    """
    图片合并 — 从 rescued HTML 的主内容区提取图片，补回 cleaned HTML 中丢失的图片。

    对照旧 content_clean_v2_node 的图片保护逻辑：
      - 使用与 _find_content_area 同款的内容区定位算法
      - 只提取正文区域内的图片（排除 header/footer/sidebar）
      - 追加到 cleaned HTML 末尾（保持正文结构完整）

    Returns:
        (merged_html, added_count, new_src_urls)
    """
    if not rescued_html or not cleaned_html:
        return cleaned_html, 0, []

    try:
        rescue_soup = BeautifulSoup(rescued_html, "html.parser")
        clean_soup = BeautifulSoup(cleaned_html, "html.parser")
    except Exception:
        return cleaned_html, 0, []

    # ── 1. 收集 cleaned HTML 中已有图片的 src ──
    clean_srcs = set()
    for tag in clean_soup.find_all(["img", "graphic"]):
        src = (tag.get("src") or "").strip()
        if src:
            clean_srcs.add(src)

    # ── 2. 从 rescued HTML 找到正文区域（规则粗筛） ──
    content = _find_main_content(rescue_soup, page_url)
    if not content:
        return cleaned_html, 0, []

    # ── 3. 从正文区提取 cleaned 中缺失的图片 ──
    missing_imgs = []
    new_urls = []
    for img in content.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        if src in clean_srcs:
            continue
        # 排除装饰图（icon/logo/国旗/备案/二维码等模板图）
        src_lower = src.lower()
        if any(kw in src_lower for kw in ("icon", "logo", "avatar", "favicon", "1x1", "pixel",
                                            "szicbok", "cn.gif", "en.gif", "tubiao",
                                            "template/default/images/")):
            continue
        # 从 template/default/images/ 中仅保留 Banner 图
        if "template/default/images/" in src_lower and "nban" not in src_lower:
            continue
        # 复制图片元素
        new_img = rescue_soup.new_tag("img", src=src)
        alt = (img.get("alt") or img.get("title") or "").strip()
        if alt:
            new_img["alt"] = alt
        new_img["loading"] = "lazy"
        new_img["referrerpolicy"] = "no-referrer"
        new_img["style"] = "max-width:100%;height:auto;display:block;margin:10px 0;"
        missing_imgs.append(new_img)
        new_urls.append(src)
        clean_srcs.add(src)

    if not missing_imgs:
        return cleaned_html, 0, []

    # ── 4. 将缺失的图片追加到 cleaned HTML 末尾 ──
    body = clean_soup.find("body") or clean_soup
    # 添加分隔线和图注
    if missing_imgs:
        hr = clean_soup.new_tag("hr")
        hr["style"] = "border:1px solid #eee;margin:20px 0;"
        body.append(hr)
        caption = clean_soup.new_tag("p")
        caption["style"] = "color:#666;font-size:12px;"
        caption.string = f"（补充图片 {len(missing_imgs)} 张）"
        body.append(caption)

    for img in missing_imgs:
        body.append(img)

    return str(clean_soup), len(missing_imgs), new_urls


def _find_main_content(soup: BeautifulSoup, page_url: str = ""):
    """
    从 rescued HTML 中定位正文区域（两层判断：规则粗筛 + LLM 精判）。

    层1 — 规则粗筛:
      1. <article> 标签
      2. 类名含 content/main/article/detail/post/entry 的容器
      3. 链接密度 > 0.3 的候选 → 直接排除（纯导航）
      4. 含 >=2 个电话号码/ICP/二维码关键词 → 排除（页脚/侧边栏）
      5. 对剩余候选按文本长度 + 图片数排序 → 取 Top 5

    层2 — LLM 精判:
      - 将 Top 5 候选的特征发给 LLM，逐个判断是否正文
      - 只保留 LLM 判定 YES 的区块
      - 全部被拒 → 回退到 body 再交给 LLM 做内容提取

    Returns:
        一个包含所有有效候选区块的 BeautifulSoup Tag（合并后的容器）
    """
    import re

    body = soup.find("body")
    if not body:
        return soup

    # ── 跳过明显是导航/侧边栏的元素 ──
    def _is_noise(el) -> bool:
        el_class = " ".join(el.get("class", [])).lower()
        el_id = (el.get("id") or "").lower()
        skip_kw = ["nav", "menu", "footer", "sidebar", "widget", "header",
                   "breadcrumb", "pagination", "comment", "ad", "ads",
                   "banner", "carousel", "slider", "popup", "modal"]
        # 按连字符/下划线/空白切分为 token 精确匹配。
        # 不能用子串匹配：Tailwind 类如 leading-relaxed 含子串 "ad"、
        # grid-cols-2 含 "list" 等，会把正文区块误判为噪音/广告整块删除。
        tokens = re.split(r"[\s\-_:./#\[\]()\"']+", el_class + " " + el_id)
        return any(kw in tokens for kw in skip_kw)

    # ── 链接密度过滤 ──
    def _link_density(el) -> float:
        text = el.get_text(strip=True)
        links = len(el.find_all("a", href=True))
        return links / max(len(text), 1)

    # ── 页脚/噪声特征检测 ──
    _PHONE_RE = re.compile(r'(电话|传真|手机|Tel|Phone|Fax)[：:\s]*[\d\-]{7,}', re.I)
    _ICP_RE = re.compile(r'(备案号|ICP|©|版权所有|Copyright)', re.I)
    _QR_RE = re.compile(r'(二维码|扫码|关注公众号|QR|WeChat)', re.I)

    def _has_footer_features(el) -> bool:
        text = el.get_text(strip=True)
        phone_count = len(_PHONE_RE.findall(text))
        icp_count = len(_ICP_RE.findall(text))
        qr_count = len(_QR_RE.findall(text))
        return (phone_count >= 2) or (icp_count >= 1 and phone_count >= 1) or (qr_count >= 1)

    # 策略1: <article> 标签（直接信任）
    article = soup.find("article")
    if article and len(article.get_text(strip=True)) > 100 and not _has_footer_features(article):
        return article

    # 策略2: 类名/id 关键字定位 — 收集 + 粗筛
    content_kw = ["content", "main-content", "article", "detail", "post",
                  "entry", "news-detail", "news-content", "pagebody",
                  "body", "text", "news", "info", "main", "container"]
    matched_blocks = []
    # 策略2.0: <main> 标签（HTML5 语义正文容器）。
    # 现代 SPA/Vue 站点的正文常整体包在 <main> 里，各 section 无 content_kw
    # 关键词（如 class="py-16 lg:py-24 bg-ocean-50"），按 class 匹配会漏掉它们。
    for el in soup.find_all("main"):
        if _is_noise(el):
            continue
        text_len = len(el.get_text(strip=True))
        if text_len < 100:
            continue
        ld = _link_density(el)
        if ld > 0.3:
            continue
        if _has_footer_features(el):
            continue
        matched_blocks.append((el, text_len, ld))
    for kw in content_kw:
        for el in soup.find_all(["div", "section", "main"], class_=re.compile(kw, re.I)):
            if _is_noise(el):
                continue
            text_len = len(el.get_text(strip=True))
            if text_len < 100:
                continue
            ld = _link_density(el)
            if ld > 0.3:  # ★ 规则粗筛：链接密度过高 → 纯导航，排除
                continue
            if _has_footer_features(el):  # ★ 页脚特征 → 排除
                continue
            matched_blocks.append((el, text_len, ld))
        for el in soup.find_all(["div", "section", "main"], id=re.compile(kw, re.I)):
            if _is_noise(el):
                continue
            text_len = len(el.get_text(strip=True))
            if text_len < 100:
                continue
            ld = _link_density(el)
            if ld > 0.3:
                continue
            if _has_footer_features(el):
                continue
            matched_blocks.append((el, text_len, ld))

    # 图片富集容器（老式表格布局：正文图片常集中在无 class/id 的 <table>/<td> 中，
    # 关键字匹配不到它们，导致正文图片被整块丢弃。图集/人物照片页尤甚。）
    for el in soup.find_all(["table", "td", "div"]):
        if _is_noise(el):
            continue
        if len(el.find_all("img")) < 3:
            continue
        if _has_footer_features(el):
            continue
        matched_blocks.append((el, len(el.get_text(strip=True)), _link_density(el)))

    # 去重
    unique_blocks = []
    for el, tlen, ld in matched_blocks:
        is_contained = False
        for other_el, _, _ in matched_blocks:
            if el is not other_el and el in other_el.descendants:
                is_contained = True
                break
        if not is_contained:
            unique_blocks.append((el, tlen, ld))

    if len(unique_blocks) >= 1:
        unique_blocks.sort(key=lambda x: x[1] + len(x[0].find_all("img")) * 100, reverse=True)
        top_blocks = unique_blocks[:5]  # ★ 取 Top 5 供 LLM 精判
        # 合并
        wrapper = soup.new_tag("div")
        for el, _, _ in top_blocks:
            clone = soup.new_tag(el.name)
            for attr, val in el.attrs.items():
                clone[attr] = val
            for child in list(el.children):
                clone.append(child)
            wrapper.append(clone)
        return wrapper

    # 策略3: 最长文本 div — Top 3
    candidates = []
    for div in body.find_all("div", recursive=True):
        if _is_noise(div):
            continue
        text_len = len(div.get_text(strip=True))
        img_count = len(div.find_all("img"))
        ld = _link_density(div)
        if ld > 0.3:  # ★ 规则粗筛
            continue
        if _has_footer_features(div):  # ★ 页脚特征
            continue
        if text_len > 100 or img_count >= 1:
            candidates.append((div, text_len, img_count))

    if candidates:
        candidates.sort(key=lambda c: c[1] + c[2] * 100, reverse=True)
        top_candidates = candidates[:3]
        wrapper = soup.new_tag("div")
        for el, _, _ in top_candidates:
            is_child = False
            for other_el, _, _ in top_candidates:
                if el is not other_el and el in other_el.descendants:
                    is_child = True
                    break
            if is_child:
                continue
            clone = soup.new_tag(el.name)
            for attr, val in el.attrs.items():
                clone[attr] = val
            for child in list(el.children):
                clone.append(child)
            wrapper.append(clone)
        return wrapper

    return body


async def _llm_judge_content_blocks(
    blocks: list, page_url: str
) -> list:
    """
    LLM 精判 — 将粗筛后的候选区块特征传给 LLM，逐个判断是否正文。

    对每个区块提取:
      - 文本长度、链接数量、图片数量
      - 前3行文本预览
      - 是否包含联系电话/地址/邮箱

    LLM 返回每个区块的 YES/NO 判定。

    Returns:
        仅保留 YES 的区块列表
    """
    if not blocks:
        return []

    llm = _get_llm()
    if not llm:
        # 无 LLM → 全保留（规则粗筛已够用）
        return blocks

    import json

    # 构建候选区块特征描述
    candidates_desc = []
    for i, (el, tlen, ld) in enumerate(blocks):
        lines = [l.strip() for l in el.get_text(strip=True).split("\n") if l.strip()][:3]
        preview = " \\n ".join(lines[:3])[:200]
        links = len(el.find_all("a", href=True))
        imgs = len(el.find_all("img"))
        has_contact = bool(re.search(
            r'(电话|传真|手机|邮箱|地址|Tel|Phone|Email|Address)',
            el.get_text(strip=True), re.I
        ))
        candidates_desc.append({
            "id": i + 1,
            "text_len": tlen,
            "link_count": links,
            "link_density": round(ld, 4),
            "img_count": imgs,
            "preview": preview,
            "has_contact_info": has_contact,
        })

    prompt = f"""你是一个网页正文区域识别专家。以下是经过规则粗筛后的候选内容区块。

网页URL: {page_url}

候选区块:
{json.dumps(candidates_desc, ensure_ascii=False, indent=2)}

请逐个判断每个区块是否为"页面正文内容区域"。
判断规则:
- 正文区域通常: 文字多、链接少、有完整段落
- 非正文区域: 导航链接密、全是标题列表、全是联系方式/版权
- 如果区块同时包含正文段落 AND 联系方式，仍应判断为 YES（联系方式可能在正文中）

请返回 JSON 格式: [{{"id": 1, "answer": "YES", "reason": "简要理由"}}, ...]

只输出 JSON，不要输出其他内容。"""

    try:
        response = llm.invoke(prompt)
        content = response.content if hasattr(response, "content") else str(response)
        # 提取 JSON
        json_match = re.search(r'\[.*\]', content, re.DOTALL)
        if json_match:
            judgments = json.loads(json_match.group())
            # 只保留 YES 的
            yes_ids = {j["id"] for j in judgments if j.get("answer", "").upper() == "YES"}
            if yes_ids:
                return [blocks[j["id"] - 1] for j in judgments if j["id"] in yes_ids]
            # 全部被拒 → 返回空，触发回退
            return []
    except Exception as e:
        agent_logger.warning(f"[LLM精判] 失败: {e}")

    # LLM 失败 → 全保留
    return blocks


# ============================================================================
# Node 1: 站点侦察
# ============================================================================

async def scout_node(state: CrawlerState) -> dict:
    """
    侦察节点 — 分析种子 URL，产出 SiteProfile 和初始配置。

    职责:
      - 用 PageScout 分析首页
      - 设置输出目录、站点名称
      - 初始化默认爬虫配置
    """
    url = state.get("seed_url", "")
    if not url:
        return {"error": "seed_url 为空"}

    agent_logger.info(f"[Graph::scout] 分析站点: {url}")

    try:
        profile = await _get_scout().analyze(url)
    except Exception as e:
        agent_logger.error(f"[Graph::scout] 分析失败: {e}")
        return {"error": str(e)}

    domain = urlparse(url).netloc.replace(":", "_")
    output_dir = os.path.join(config.LOCAL_BACKUP_DIR, domain)
    os.makedirs(output_dir, exist_ok=True)

    default_config = CrawlerConfig(
        needs_js_render=profile.needs_js_render,
    )

    agent_logger.info(
        f"[Graph::scout] 完成 | title={profile.title[:30]} | "
        f"js={profile.needs_js_render} | type={profile.site_type}"
    )

    return {
        "site_profile": profile.model_dump(),
        "site_name": profile.title[:60],
        "output_dir": output_dir,
        "crawler_config": default_config.model_dump(),
        "seen_url_keys": [],
        "seen_hashes": [],
        "queue": [],
        "crawled_results": [],
        "stats": {
            "scouted": 1, "fetched": 0, "extracted": 0,
            "saved": 0, "skipped": 0, "duplicate": 0, "failed": 0,
        },
        "adjustment_count": 0,
        "error": "",
    }


# ============================================================================
# Node 2: 导航提取
# ============================================================================

async def navigate_node(state: CrawlerState) -> dict:
    """
    导航节点 — 抓取首页, 提取导航栏中的子链接填充 BFS 队列。

    职责:
      - 调用 FetcherRouter 获取首页 HTML
      - 用 NavigationParser 提取导航链接
      - 所有链接入队（queue）
    """
    url = state.get("seed_url", "")
    profile_dict = state.get("site_profile", {})
    output_dir = state.get("output_dir", "")

    if not url or not profile_dict:
        return {"error": "scout 未完成，缺少 seed_url 或 site_profile"}

    profile = SiteProfile(**profile_dict)
    agent_logger.info(f"[Graph::navigate] 抓取首页: {url}")

    # 抓取首页
    fetcher = _get_fetcher(state.get("crawler_config", {}))
    homepage = await fetcher.fetch(url, profile)
    if not homepage.html or len(homepage.html) < 100:
        agent_logger.error(f"[Graph::navigate] 首页抓取失败, len={len(homepage.html)}")
        return {"error": f"首页抓取失败: HTML 长度 {len(homepage.html)}"}

    agent_logger.info(
        f"[Graph::navigate] 首页抓取成功 | method={homepage.fetch_method} | "
        f"html_len={len(homepage.html)}"
    )

    # 提取导航链接
    nav = _get_nav()
    hp_links = await nav.extract_links(homepage.html, profile, current_depth=0)
    agent_logger.info(f"[Graph::navigate] 导航链接: {len(hp_links)} 个")

    # 构建初始队列
    base_host = urlparse(url).netloc.lower()
    queue: List[Dict[str, Any]] = []
    seen_keys: List[str] = list(state.get("seen_url_keys", []))

    for link in hp_links:
        key = _url_key(link.url)
        if key not in seen_keys:
            seen_keys.append(key)
            queue.append({
                "url": link.url,
                "depth": link.depth,
                "nav_path": link.nav_path,
                "is_homepage": False,
            })

    # 也把首页加入 seen
    hp_key = _url_key(url)
    if hp_key not in seen_keys:
        seen_keys.append(hp_key)

    agent_logger.info(f"[Graph::navigate] 入队 {len(queue)} 个链接")
    return {
        "queue": queue,
        "seen_url_keys": seen_keys,
        "current_html": homepage.html,
        "current_url": url,
    }


# ============================================================================
# Node 3: 抓取 + 清洗（传统爬虫核心，每次处理队列中一个 URL）
# ============================================================================

async def fetch_extract_node(state: CrawlerState) -> dict:
    """
    抓取+清洗节点 — 这是整个流程的核心执行者。

    每次调用处理 queue 中的第一个 URL:
      1. 出队一个 URL
      2. FetcherRouter 抓取页面
      3. 判断列表页/详情页:
         - 列表页 (depth < 4): 提取 body 链接 → 追加到 queue
         - 详情页: ExtractorAgent 清洗 → 生成 CSV 行 → 追加到 crawled_results
         - 列表页 (depth >= 4): 丢弃
      4. MD5 内容去重
    """
    queue: List[Dict] = list(state.get("queue", []))
    profile_dict = state.get("site_profile", {})
    output_dir = state.get("output_dir", "")
    site_name = state.get("site_name", "")
    seed_url = state.get("seed_url", "")
    config_dict = state.get("crawler_config", {})
    seen_keys: List[str] = list(state.get("seen_url_keys", []))
    retry_map: Dict[str, int] = dict(state.get("url_retry_count", {}))
    seen_hashes: List[str] = list(state.get("seen_hashes", []))
    stats: Dict[str, int] = dict(state.get("stats", {}))
    extraction_rules = state.get("extraction_rules", {})  # LLM 生成的规则
    # ★ 已内嵌图片的页面 URL（media 阶段处理过），其 HTML 不应再被 re-enqueue 覆盖
    media_processed_keys: set = {_url_key(u) for u in (state.get("media_processed_urls", []) or []) if u}

    if not queue:
        return {"error": "queue 为空，无需处理"}

    profile = SiteProfile(**profile_dict) if profile_dict else None
    if not profile:
        return {"error": "site_profile 缺失"}

    # ── 出队 ──
    item = queue.pop(0)
    url = item["url"]
    depth = item.get("depth", 1)
    nav_path = item.get("nav_path", [])

    agent_logger.info(f"[Graph::fetch_extract] depth={depth} | {url[:80]}")

    # ── 抓取 ──
    fetcher = _get_fetcher(config_dict)
    try:
        page = await fetcher.fetch(url, profile)
        page.nav_path = nav_path
        page.depth = depth
        stats["fetched"] = stats.get("fetched", 0) + 1
    except Exception as e:
        agent_logger.warning(f"[Graph::fetch_extract] 抓取失败: {e}")
        stats["failed"] = stats.get("failed", 0) + 1
        key = _url_key(url)
        retries = retry_map.get(key, 0) + 1
        if retries <= MAX_RETRY_COUNT:
            # 允许重试：从 seen 移除，重新入队
            retry_map[key] = retries
            if key in seen_keys:
                seen_keys.remove(key)
            queue.append({"url": url, "depth": depth, "nav_path": nav_path, "_retries": retries})
            agent_logger.info(f"[Graph::fetch_extract] 重试 {retries}/{MAX_RETRY_COUNT} | {url[:60]}")
        else:
            retry_map[key] = retries
            agent_logger.info(f"[Graph::fetch_extract] 已达最大重试 ({MAX_RETRY_COUNT})，放弃 | {url[:60]}")
        return {"queue": queue, "stats": stats, "seen_url_keys": seen_keys,
                "url_retry_count": retry_map}

    if not page.html or len(page.html) < 100:
        stats["failed"] = stats.get("failed", 0) + 1
        agent_logger.warning(f"[Graph::fetch_extract] HTML 过短 ({len(page.html)})")
        key = _url_key(url)
        retries = retry_map.get(key, 0) + 1
        if retries <= MAX_RETRY_COUNT:
            retry_map[key] = retries
            if key in seen_keys:
                seen_keys.remove(key)
            queue.append({"url": url, "depth": depth, "nav_path": nav_path, "_retries": retries})
            agent_logger.info(f"[Graph::fetch_extract] 重试 {retries}/{MAX_RETRY_COUNT} | {url[:60]}")
        else:
            retry_map[key] = retries
        return {"queue": queue, "stats": stats, "seen_url_keys": seen_keys,
                "url_retry_count": retry_map}

    # ── 反爬拦截检测：命中则直接丢弃，不重试 ──
    blocked, block_reason = _detect_anti_crawl_block(
        page.html or "", getattr(page, "fetch_method", "")
    )
    if blocked:
        import logging
        logging.debug(f"[AntiCrawl Detail] URL={url}, 原因={block_reason}")
        agent_logger.warning(
            f"[Graph::fetch_extract] 反爬拦截: 高级反爬爬不了 | {url[:80]}"
        )
        stats["failed"] = stats.get("failed", 0) + 1
        blocked_urls = dict(state.get("anti_crawl_blocked_urls", {}))
        blocked_urls[_url_key(url)] = block_reason
        return {
            "queue": queue, "stats": stats, "seen_url_keys": seen_keys,
            "url_retry_count": retry_map,
            "anti_crawl_blocked_urls": blocked_urls,
        }

    # ── 图片抢救：修复路径/懒加载/CSS背景图/防盗链（必须在清洗之前执行） ──
    loop = asyncio.get_running_loop()
    raw_html = page.html  # 保留原始 HTML（分页提取等需要未处理的 DOM）
    rescued, img_stats = await loop.run_in_executor(None, _rescue_images, page.html, url)
    rescued_html = rescued  # 保存抢救后的原始 HTML（后续图片合并用）
    page.html = rescued

    # ── 列表页判断 ──
    base_host = urlparse(seed_url).netloc.lower()

    text_for_check = await loop.run_in_executor(
        None, lambda: BeautifulSoup(page.html, "html.parser").find("body")
    )
    text_for_check = text_for_check.get_text(" ", strip=True) if text_for_check else ""
    is_list, list_conf, reason = _is_list_page(page.html, text_for_check)

    # 标记是否来自列表页分支（用于二次拦截时的内容回退）
    _from_list = is_list

    if is_list and depth >= 4:
        # 超深列表页 → 丢弃
        stats["skipped"] = stats.get("skipped", 0) + 1
        return {"queue": queue, "stats": stats, "seen_url_keys": seen_keys}

    if is_list and depth < 4:
        # ★ 正文页误判保护：如果页面有大量正文内容（>5000字），说明是详情页（非真列表页），不提取子链接
        #    避免侧边栏推荐/相关链接等高密度区导致 nav_path 传播污染
        if len(text_for_check) > 5000:
            agent_logger.info(
                f"[Graph::fetch_extract] 列表页判为正文页(正文{len(text_for_check)}字)，跳过子链接提取 "
                f"| reason={reason}"
            )
        else:
            body_links = await loop.run_in_executor(
                None, _extract_body_links, page.html, url, base_host
            )
            # ★ 提取 JS 驱动的分页链接（如 TRS CMS 的 onclick 分页）
            # 使用 raw_html（rescue 前）避免 onclick/tagname 属性丢失
            pagination_links = await loop.run_in_executor(
                None, _extract_pagination_links, raw_html, url, base_host
            )
            all_links = body_links + pagination_links
            added = 0
            re_enqueued = 0
            new_depth = depth + 1
            for abs_url, link_text in all_links:
                key = _url_key(abs_url)
                if key not in seen_keys:
                    seen_keys.append(key)
                    clean_text = (link_text or "").lstrip("> \t\r\n")[:20]
                    # 过滤纯数字分页链接（如 "1", "2", "12" 等），不参与 nav_path
                    if clean_text and _re.match(r'^\d{1,2}$', clean_text):
                        clean_text = ""
                    # ★ 过滤超长链接文本（>15 字 → 文章标题，非导航标签），不参与 nav_path
                    if clean_text and len(clean_text) > 15:
                        clean_text = ""
                    queue.append({
                        "url": abs_url,
                        "depth": new_depth,
                        "nav_path": nav_path,
                    })
                    added += 1
                # ★ BFS 重入队：URL 已发现但当前 nav_path 更好时，允许重新入队
                elif _should_re_enqueue(key, nav_path):
                    if key in media_processed_keys:
                        # ★ 已内嵌 Base64 的页面禁止重入队覆盖（否则图片会被远程 URL 覆盖回去，白内嵌）
                        continue
                    # 已保存过的清除URL去重记录，让它重新保存到正确目录
                    _saved_urls.discard(key)
                    queue.append({
                        "url": abs_url,
                        "depth": new_depth,
                        "nav_path": nav_path,
                        "_re_enqueued": True,
                    })
                    re_enqueued += 1
            if added or pagination_links or re_enqueued:
                log_parts = [f"列表页 depth={depth} → +{added} 子链接"]
                if pagination_links:
                    log_parts.append(f"+{len(pagination_links)}分页")
                if re_enqueued:
                    log_parts.append(f"重入队{re_enqueued}")
                log_parts.append(f"| reason={reason}")
                agent_logger.info(
                    f"[Graph::fetch_extract] {' '.join(log_parts)}"
                )
        # ★ 不 return — 继续往下保存列表页自身内容（防止"企业环境"等含内容的栏目页丢失）

    # ── 详情页: 清洗 ──
    # 优先使用 LLM 生成的规则（如果存在），否则使用默认 trafilatura/BS4 管道
    if extraction_rules:
        agent_logger.info(f"[Graph::fetch_extract] 使用 LLM 定制规则提取")
        loop = asyncio.get_running_loop()
        cleaned_html, text_content, img_count = await loop.run_in_executor(
            None, _extract_with_rules, page.html, url, extraction_rules
        )
        # 构建简化的 PageData 供后续处理
        cleaned = PageData(
            url=url,
            title=page.title or "",
            html=cleaned_html,
            nav_path=nav_path,
            depth=depth,
            images_count=img_count,
            content_hash=_compute_md5(text_content),
            is_list_page_detected_at_extract=(len(text_content) < 50),
        )
        # 收集图片 URL
        _, img_urls, img_alts = _collect_images(cleaned_html)
        cleaned.images_urls = img_urls
        cleaned.images_alts = img_alts
    else:
        extractor = _get_extractor()
        try:
            if is_list:
                # ★ 列表页/栏目页提速：跳过 extractor 的 LLM 深降级（列表页无需精确正文，
                #   trafilatura+BS4 低置信度时直接调 LLM 每页十几秒且烧 token），改用 BS4 清洗。
                cleaned_html, text_content, _, _ = await loop.run_in_executor(
                    None, _extract_with_bs4, page.html, url
                )
                cleaned = PageData(
                    url=url,
                    title=page.title or "",
                    html=cleaned_html or "",
                    nav_path=nav_path,
                    depth=depth,
                    images_count=len(re.findall(r'<img\b', cleaned_html or "", re.I)),
                    content_hash=_compute_md5(text_content or ""),
                    is_list_page_detected_at_extract=(len(text_content or "") < 50),
                )
                agent_logger.info(
                    f"[Graph::fetch_extract] 列表页使用 BS4 清洗(跳过 LLM 深降级) | {url[:60]}"
                )
            else:
                cleaned = await extractor.extract(page, profile)
            stats["extracted"] = stats.get("extracted", 0) + 1
        except Exception as e:
            agent_logger.warning(f"[Graph::fetch_extract] 清洗失败: {e}")
            stats["failed"] = stats.get("failed", 0) + 1
            return {"queue": queue, "stats": stats, "seen_url_keys": seen_keys}

    # ★ 列表页/详情页二次拦截处理：提取器返回空时，使用抢救后的原始 HTML 作为内容
    if cleaned.is_list_page_detected_at_extract and rescued_html and len(rescued_html) > 200:
        # 用 rescued HTML 替代空内容 — 使用 BS4 做基础清洗后保存
        cleaned_html, text_content, _, _ = await loop.run_in_executor(
            None, _extract_with_bs4, rescued_html, url
        )
        if cleaned_html and len(cleaned_html.strip()) >= 50:
            # 重新收集图片信息
            img_cnt, img_urls, img_alts = await loop.run_in_executor(
                None, _collect_images, cleaned_html
            )
            cleaned.html = cleaned_html
            cleaned.images_count = img_cnt
            cleaned.images_urls = img_urls[:20]
            cleaned.images_alts = img_alts[:20]
            cleaned.content_hash = _compute_md5(text_content)
            cleaned.is_list_page_detected_at_extract = False
            agent_logger.info(
                f"[Graph::fetch_extract] 列表页使用 rescued HTML 替代 | "
                f"text_len={len(text_content)} | imgs={img_cnt}"
            )

    if cleaned.is_list_page_detected_at_extract or (
        not cleaned.html or len(cleaned.html.strip()) < 50
    ):
        # 二次机会：如果 rescued HTML 足够长，直接进入模板构建
        if rescued_html and len(rescued_html) >= 200:
            agent_logger.info(
                f"[Graph::fetch_extract] 提取内容过短({len(cleaned.html.strip()) if cleaned.html else 0}B)，"
                f"使用 rescued HTML 构建模板 | {url[:60]}"
            )
        else:
            stats["skipped"] = stats.get("skipped", 0) + 1
            return {"queue": queue, "stats": stats, "seen_url_keys": seen_keys}

    # ── 构建模板 HTML（结构化 + 固定排版）──
    structured_body = _build_structured_content(rescued_html, url)

    # ── 内容有效性检查：正文过短（纯二维码/空页面）→ 丢弃 ──
    _check_text = re.sub(r'<[^>]+>', '', structured_body).strip()
    _check_text = re.sub(r'\s+', '', _check_text)  # 去掉所有空白
    if len(_check_text) < 80:
        agent_logger.info(
            f"[Graph::fetch_extract] 正文内容过短({len(_check_text)}字)，"
            f"丢弃空/二维码页面 | {url[:60]}"
        )
        stats["skipped"] = stats.get("skipped", 0) + 1
        return {"queue": queue, "stats": stats, "seen_url_keys": seen_keys}

    # ── 去重（基于最终结构化正文，避免短文本/空内容页被误判为重复） ──
    content_hash = _compute_md5(structured_body + "\x00" + (cleaned.title or ""))
    if content_hash and content_hash in seen_hashes:
        stats["duplicate"] = stats.get("duplicate", 0) + 1
        agent_logger.info(f"[Graph::fetch_extract] 重复跳过 | hash={content_hash[:8]}")
        # 仍然写一条跳过记录到 CSV
        csv_row = _build_skipped_csv_row(url, site_name, nav_path, cleaned.title,
                                          "duplicate", f"hash={content_hash[:8]}")
        return {
            "queue": queue, "stats": stats, "seen_url_keys": seen_keys,
            "crawled_results": [csv_row],
        }

    if content_hash:
        seen_hashes.append(content_hash)

    # ── nav_path 补全（优先级：BFS传递 > 面包屑 ≥2级 > URL路径推断 > 面包屑单级） ──
    if rescued_html and (not nav_path or nav_path == ["网站地图"]):
        breadcrumb_nav = _extract_breadcrumb_nav(rescued_html)
        if breadcrumb_nav and len(breadcrumb_nav) >= 2:
            # 面包屑提供 ≥2 级层级 → 直接采用
            merged = list(breadcrumb_nav)
            for p in nav_path:
                if p and p != "网站地图" and p not in merged:
                    merged.append(p)
            if merged and merged != nav_path:
                agent_logger.info(
                    f"[Graph::fetch_extract] 面包屑推断 nav_path(≥2级): "
                    f"{nav_path} → {merged} | {url[:60]}"
                )
                nav_path = merged
                cleaned.nav_path = list(merged)
        elif breadcrumb_nav and not _is_likely_page_title(breadcrumb_nav[0]):
                # 面包屑单级但非页面标题 → 可作为 nav_path 兜底
                merged = list(breadcrumb_nav)
                for p in nav_path:
                    if p and p != "网站地图" and p not in merged:
                        merged.append(p)
                if merged and merged != nav_path:
                    agent_logger.info(
                        f"[Graph::fetch_extract] 面包屑兜底 nav_path(单级): "
                        f"{nav_path} → {merged} | {url[:60]}"
                    )
                    nav_path = merged
                    cleaned.nav_path = list(merged)
    elif rescued_html:
        # BFS nav_path 已有有效值 → 仅当面包屑明显更深时才合并
        breadcrumb_nav = _extract_breadcrumb_nav(rescued_html)
        if breadcrumb_nav and len(breadcrumb_nav) > len(nav_path):
            merged = list(breadcrumb_nav)
            for p in nav_path:
                if p and p != "网站地图" and p not in merged:
                    merged.append(p)
            if merged and merged != nav_path:
                agent_logger.info(
                    f"[Graph::fetch_extract] 面包屑增强 nav_path: "
                    f"{nav_path} → {merged} | {url[:60]}"
                )
                nav_path = merged
                cleaned.nav_path = list(merged)

    template_html = _build_template_html(
        structured_body=structured_body,
        title=cleaned.title,
        nav_path=nav_path,
        source_url=url,
    )

    # ── 清洗最后：去除导航栏残留（面包屑/上下篇/分页 + 模板面包屑） ──
    template_html = _strip_nav_noise(template_html)

    cleaned.html = template_html

    # ── 从最终模板 HTML 重新收集图片 ──
    # trafilatura 清洗会剥离 <img> 标签，导致 extractor 阶段 images_urls 为空，
    # 进而 CSV 的 download_img_url 为空（"图片爬不出来"）。此处以最终模板为准重新收集。
    try:
        _tmpl_img_cnt, _tmpl_img_urls, _tmpl_img_alts = _collect_images(template_html)
        cleaned.images_count = _tmpl_img_cnt
        cleaned.images_urls = _tmpl_img_urls[:20]
        cleaned.images_alts = _tmpl_img_alts[:20]
    except Exception:
        pass

    # ── 构建 CSV 行数据 ──
    csv_row = _build_csv_row(cleaned, site_name, nav_path)

    # ── 保存 HTML 文件到磁盘 ──
    rel_path = await _save_html_file(cleaned, output_dir)
    csv_row["file_path"] = rel_path

    stats["saved"] = stats.get("saved", 0) + 1
    agent_logger.info(
        f"[Graph::fetch_extract] 已保存 | title={cleaned.title[:30]} | "
        f"imgs={cleaned.images_count} | hash={content_hash[:8] if content_hash else 'N/A'}"
    )

    return {
        "queue": queue,
        "stats": stats,
        "seen_url_keys": seen_keys,
        "seen_hashes": seen_hashes,
        "crawled_results": [csv_row],
    }


# ============================================================================
# Node 4: LLM 评估（仅在传统爬虫完成 queue 后调用）
# ============================================================================

async def evaluate_node(state: CrawlerState) -> dict:
    """
    LLM 评估节点 — 检查传统爬虫的输出质量。

    仅在 queue 为空时被路由到此节点。
    使用 LLM 对 crawled_results 做整体评估:
      - 内容质量（文本长度、结构）
      - 反爬检测（大量空页/短页）
      - 图片提取完整性
      - 导航覆盖度

    返回 {passed, score, issues, suggestion} → 条件路由决定下一步。
    """
    results: List[Dict] = list(state.get("crawled_results", []))
    stats = state.get("stats", {})
    site_name = state.get("site_name", "")
    adjustment_count = state.get("adjustment_count", 0)
    seed_url = state.get("seed_url", "")

    # ── 无 LLM 时的降级：纯启发式评估 ──
    llm = _get_llm()
    if llm is None:
        agent_logger.info("[Graph::evaluate] 无 LLM，使用启发式评估")
        evaluation = _heuristic_evaluate(results, stats)
    else:
        try:
            evaluation = await _llm_evaluate(llm, results, stats, site_name, seed_url)
        except Exception as e:
            agent_logger.warning(f"[Graph::evaluate] LLM 评估失败: {e}，降级为启发式")
            evaluation = _heuristic_evaluate(results, stats)

    agent_logger.info(
        f"[Graph::evaluate] passed={evaluation.passed} | score={evaluation.score:.2f} | "
        f"adjustment_count={adjustment_count} | {evaluation.summary}"
    )

    return {
        "evaluation": evaluation.model_dump(),
        "adjustment_count": adjustment_count,  # 不在这里递增
    }


# ============================================================================
# Node 5: 配置调整
# ============================================================================

async def config_adjust_node(state: CrawlerState) -> dict:
    """
    配置调整节点 — 根据 LLM 评估建议，修改爬虫配置后重新入队。

    只做参数调整（UA / JS渲染 / Headers / Cookies），不生成代码。
    """
    evaluation_dict = state.get("evaluation", {})
    config_dict = dict(state.get("crawler_config", {}))
    adjustment_count = state.get("adjustment_count", 0) + 1
    seed_url = state.get("seed_url", "")
    profile_dict = state.get("site_profile", {})

    evaluation = EvaluationResult(**evaluation_dict) if evaluation_dict else None
    if not evaluation:
        return {"error": "无评估结果，无法调整"}

    agent_logger.info(
        f"[Graph::adjust] 第 {adjustment_count} 次调整 | "
        f"needs_js={evaluation.needs_js_render} | ua={bool(evaluation.recommended_ua)}"
    )

    # 应用 LLM 建议
    config = CrawlerConfig(**config_dict)
    if evaluation.needs_js_render:
        config.needs_js_render = True
    if evaluation.recommended_ua:
        config.user_agent = evaluation.recommended_ua
    if evaluation.recommended_headers:
        config.extra_headers = evaluation.recommended_headers

    agent_logger.info(f"[Graph::adjust] 新配置: js={config.needs_js_render} | ua={config.user_agent[:40] if config.user_agent else 'default'}")

    # 重建队列：只从种子 URL 重新开始
    queue: List[Dict] = [{
        "url": seed_url,
        "depth": 1,
        "nav_path": [],
        "is_homepage": True,
    }]

    return {
        "crawler_config": config.model_dump(),
        "adjustment_count": adjustment_count,
        "queue": queue,
        "seen_url_keys": [_url_key(seed_url)],
        "crawled_results": [],  # 清空，重新抓取
    }


# ============================================================================
# Node 6: 媒体处理器 — 图片 Base64 内嵌
# ============================================================================

# 图片下载并发控制
_IMG_SEMAPHORE = asyncio.Semaphore(30)
# 可被 Base64 替换的图片标签
_IMG_TAGS = {"img", "graphic", "image"}

# 图标/装饰性图片的特征（不转 Base64）
_ICON_PATTERNS = [
    "icon", "logo", "avatar", "banner-ad", "pixel", "tracking",
    "spacer", "blank", "dot", "bullet", "arrow",
    "favicon", "btn", "button", "qr-code", "qrcode",
    "erweima", "二维码", "wechat", "badge", "cert",
    # ★ 装饰小图（harbin 列表图标类）：直接按文件名后缀拦截，避免误伤含 ico 的正文词
    "ico.jpg", "ico.jpeg", "ico.png", "ico.gif", "ico.bmp",
    "ico_", "_ico", "/ico/", "/icons/",
]

# ★ 图片 URL 强制过滤关键词（绝对不要的图片）
_IMG_BLOCK_URL_KW = {
    "logo", "icon", "qrcode", "erweima", "二维码", "wechat",
    "avatar", "favicon",
}

# ★ 图片 alt 文本可疑模式（纯短数字/英文 = 低信息量）
import re as _re
_IMG_SUSPICIOUS_ALT_RE = _re.compile(r'^[a-zA-Z0-9_\-\s\.]{1,9}$')

# 最大图片大小 2MB（超过则保留原始链接）
_MAX_IMAGE_BYTES = 2 * 1024 * 1024

# 失败图片占位符（内嵌 SVG: 灰色背景 + "图片加载失败" 提示）
_FAILED_IMG_PLACEHOLDER = (
    "data:image/svg+xml;charset=utf-8,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' width='300' height='150'%3E"
    "%3Crect width='300' height='150' fill='%23f5f5f5' stroke='%23ddd' stroke-width='1'/%3E"
    "%3Ctext x='50%25' y='45%25' text-anchor='middle' font-family='sans-serif' font-size='14' fill='%23999'%3E"
    "%E5%9B%BE%E7%89%87%E5%8A%A0%E8%BD%BD%E5%A4%B1%E8%B4%A5"
    "%3C/text%3E"
    "%3C/svg%3E"
)


async def media_processor_node(state: CrawlerState) -> dict:
    """
    媒体处理节点 — 将已爬取页面的图片下载并转为 Base64 内嵌到 HTML 中。

    处理流程:
      1. 遍历 crawled_results 中所有已保存的页面
      2. 解析每个页面的 <img>/<graphic> 标签
      3. 过滤小图标/装饰图 + 下载图片二进制
      4. 检测 MIME 类型，转为 data:image/xxx;base64,...
      5. LLM 兜底：下载失败的图片交给 LLM 分析
      6. 将已处理页面的 HTML 重新写入文件（覆盖之前保存的版本）
    """
    results: List[Dict] = list(state.get("crawled_results", []))
    output_dir = state.get("output_dir", "")
    seed_url = state.get("seed_url", "")
    config_dict = state.get("crawler_config", {})

    if not results:
        return {}

    user_agent = config_dict.get("user_agent") or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    # 从配置继承 Cookie / 额外 Header（播放器重试后可能有新 UA/Cookie）
    extra_headers = config_dict.get("extra_headers", {})
    default_cookies = config_dict.get("cookies", {})  # 配置级兜底 cookie

    agent_logger.info(
        f"[Graph::media] 开始处理 {len(results)} 个页面的图片 | "
        f"ua={user_agent[:30]}... cookies={'yes' if default_cookies else 'no'}"
    )

    total_processed = 0
    total_failed = 0

    # ★ 从状态读取全局图片去重字典
    global_seen_hashes: Dict[str, int] = dict(state.get("global_seen_img_hashes", {}) or {})

    # ★ 页面级并发处理（每页内部图片已并发 15；页面之间再并发 5，提速 media 阶段）
    _MEDIA_PAGE_SEMAPHORE = asyncio.Semaphore(5)

    # 记录已处理（已内嵌）的页面 URL，供后续 re-enqueue 时避免覆盖
    media_processed_urls: List[str] = []

    async def _process_one(i: int, row: dict):
        nonlocal total_processed, total_failed
        if not row or not isinstance(row, dict):
            return
        html_src = row.get("html", "")
        if not html_src:
            return

        url = row.get("url", "") or ""
        # 使用当前页面的 URL 作为 Referer（而不全局的种子 URL）
        page_referer = url or seed_url

        # ★ 优先使用该页面抓取时捕获的 Cookie，兜底配置级 Cookie
        row_cookies = row.get("_cookies", {}) or {}
        page_cookies = row_cookies if row_cookies else default_cookies

        async with _MEDIA_PAGE_SEMAPHORE:
            try:
                html_new, processed, failed = await _embed_images_in_html(
                    html_src, url, page_referer, user_agent, extra_headers, page_cookies,
                    global_seen_hashes,
                )
                results[i]["html"] = html_new
                total_processed += processed
                total_failed += failed
                if url:
                    media_processed_urls.append(url)

                if processed or failed:
                    agent_logger.info(
                        f"[Graph::media] [{i+1}/{len(results)}] "
                        f"embedded={processed} failed={failed} | {row.get('title', url)[:40]}"
                    )
            except Exception as e:
                agent_logger.warning(f"[Graph::media] 处理失败: {e} | {url[:60]}")

    await asyncio.gather(*[
        _process_one(i, row) for i, row in enumerate(results)
        if row and isinstance(row, dict) and row.get("html")
    ])

    # ── 重新写入 HTML 文件（覆盖 Base64 之前的版本） ──
    _re_write_html_files(results, output_dir)

    agent_logger.info(
        f"[Graph::media] 完成 | embedded={total_processed} failed={total_failed} | {len(results)} 页"
    )

    return {
        # ★ 不返回 crawled_results：该字段被 _list_append 合并，返回完整列表会导致行数翻倍。
        #   处理后的行写入 media_results（普通字段，last-write-wins 覆盖），storage 优先读取。
        "media_results": results,
        "global_seen_img_hashes": global_seen_hashes,
        "media_processed_urls": media_processed_urls,
    }


# ============================================================================
# 图片 Base64 嵌入核心逻辑
# ============================================================================

async def _embed_images_in_html(
    html: str, page_url: str, referer: str,
    user_agent: str, extra_headers: dict, cookies: dict = None,
    global_seen_hashes: Optional[Dict[str, int]] = None,
) -> Tuple[str, int, int]:
    """
    解析 HTML 中所有图片标签，下载后转为 Base64 内嵌。

    Args:
        cookies: 原始请求的 session cookie，用于防盗链绕过
        global_seen_hashes: 全局图片 MD5 → 次数，用于跨页面去重

    Returns:
        (new_html, processed_count, failed_count)
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return html, 0, 0

    # 收集所有需要处理的图片标签
    tasks: List[dict] = []
    for tag in soup.find_all(list(_IMG_TAGS)):
        src = _extract_src(tag)
        if not src:
            continue
        if not _should_embed(src, tag):
            continue
        tasks.append({"tag": tag, "src": src, "page_url": page_url})

    if not tasks:
        return html, 0, 0

    # 并发下载
    async def download_one(item: dict) -> Tuple[Optional[str], str]:
        async with _IMG_SEMAPHORE:
            return await _download_and_encode(
                item["src"], item["page_url"], referer, user_agent, extra_headers, cookies or {},
                global_seen_hashes,
            )

    results = await asyncio.gather(*[download_one(t) for t in tasks], return_exceptions=True)

    processed = 0
    failed_urls: List[str] = []

    for item, result in zip(tasks, results):
        if isinstance(result, Exception):
            tag_ref = item["tag"]
            orig_src = tag_ref.get("src", tag_ref.get("data-src", ""))
            tag_ref["src"] = _FAILED_IMG_PLACEHOLDER
            tag_ref["alt"] = f"[图片加载失败 - {orig_src[:60]}]" if orig_src else "[图片加载失败]"
            tag_ref["data-original-src"] = orig_src
            tag_ref["style"] = "border:1px dashed #ddd;opacity:0.7;max-width:300px;"
            failed_urls.append(item["src"])
            continue

        b64_data, mime = result
        if b64_data:
            tag_ref = item["tag"]
            for attr in ("src", "data-src", "data-original"):
                if tag_ref.get(attr):
                    tag_ref[attr] = f"data:{mime};base64,{b64_data}"
                    for la in ("data-src", "data-original", "data-lazy-src", "data-url"):
                        if la != attr and tag_ref.has_attr(la):
                            del tag_ref[la]
                    break
            if tag_ref.has_attr("srcset"):
                del tag_ref["srcset"]
            processed += 1
        else:
            # 下载失败 → 占位图 + 保留原始链接
            tag_ref = item["tag"]
            orig_src = tag_ref.get("src", tag_ref.get("data-src", ""))
            tag_ref["src"] = _FAILED_IMG_PLACEHOLDER
            tag_ref["alt"] = f"[图片加载失败 - {orig_src[:60]}]" if orig_src else "[图片加载失败]"
            tag_ref["data-original-src"] = orig_src  # 保留原始链接供参考
            tag_ref["style"] = "border:1px dashed #ddd;opacity:0.7;max-width:300px;"
            for la in ("data-src", "data-original", "data-lazy-src", "data-url", "srcset"):
                if la != "data-original-src" and tag_ref.has_attr(la):
                    del tag_ref[la]
            failed_urls.append(item["src"])

    html_new = str(soup)

    # ── LLM 兜底已移除：失败图片统一用占位图（每页失败图再调 LLM 分析既慢又烧 token，
    #    且对 meta refresh 反爬类失败几乎无收益） ──
    # if failed_urls:
    #     llm_fixes = await _llm_analyze_failed_images(html, failed_urls, page_url)
    #     for orig_url, new_src in llm_fixes.items():
    #         html_new = html_new.replace(orig_url, new_src)

    return html_new, processed, len(failed_urls)


def _extract_src(tag) -> str:
    """从标签中提取图片 URL（优先级: src > data-src > data-original > data-lazy-src > data-url > srcset）"""
    for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-url"):
        val = tag.get(attr, "").strip()
        if val and not val.startswith("data:"):
            return val
    # srcset 第一项
    srcset = tag.get("srcset", "")
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        if first and not first.startswith("data:"):
            return first
    return ""


def _should_embed(src: str, tag=None) -> bool:
    """判断图片是否值得 Base64 嵌入（URL关键词 + alt文本 + 装饰图过滤）"""
    src_lower = src.lower()
    # 跳过已内嵌的 data URI
    if src_lower.startswith("data:"):
        return False
    # ★ URL 关键词强制过滤（Logo/二维码/图标等）
    for pat in _ICON_PATTERNS:
        if pat in src_lower:
            return False
    # 跳过极小文件（跟踪像素等，文件名含 1x1/pixel 等）
    if any(kw in src_lower for kw in ("1x1", "pixel", "blank.gif", "spacer.gif")):
        return False
    # ★ alt 文本辅助判断：纯短数字/英文 alt → 低信息量，但配合 URL 综合判断
    if tag:
        alt = (tag.get("alt") or tag.get("title") or "").strip()
        if alt and _IMG_SUSPICIOUS_ALT_RE.match(alt):
            # alt 低信息量 AND URL 含可疑关键词 → 双重确认后排除
            url_has_any_block_kw = any(kw in src_lower for kw in _IMG_BLOCK_URL_KW)
            if url_has_any_block_kw:
                return False
    return True


async def _download_and_encode(
    src: str, page_url: str, referer: str,
    user_agent: str, extra_headers: dict, cookies: dict = None,
    global_seen_hashes: Optional[Dict[str, int]] = None,
) -> Tuple[Optional[str], str]:
    """
    下载图片并编码为 Base64 + 文件特征过滤 + 全局去重。

    防盗链绕过:
      - 自动携带原页面的 Referer（使用当前页面 URL，而非种子 URL）
      - 继承原始请求的 Cookie
      - 伪装标准 Chrome UA

    图片过滤:
      - URL 关键词判断（logo/icon/qrcode/二维码）
      - 文件特征辅助（正方形 + <30KB → 疑似 Logo/图标）
      - 全局 MD5 去重（同一图片 ≥ 3 页面出现 → 全站共用图，跳过）

    Returns:
        (base64_string | None, mime_type)
    """
    # 补全相对路径
    if not src.startswith(("http://", "https://")):
        src = urljoin(page_url, src)

    headers = {
        "User-Agent": user_agent,
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        **extra_headers,
    }
    # 添加 Cookie（来自原始页面请求的 session）
    cookie_dict = cookies or {}
    if cookie_dict:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0), follow_redirects=True) as client:
            resp = await client.get(src, headers=headers)
            resp.raise_for_status()

            content = resp.content

            # ★ meta refresh 反爬：部分站点（如 harbin-electric.com）对图片首次请求
            #   返回 <meta http-equiv="refresh" content="1; URL=..."> 跳转页，
            #   httpx 不执行 meta refresh，需解析目标 URL 并延迟后重试才返回真实图片。
            for _attempt in range(3):
                if not _looks_like_html(content):
                    break
                redirect_url = _extract_meta_refresh_url(content)
                if not redirect_url:
                    break
                await asyncio.sleep(1.2)
                target = urljoin(src, redirect_url)
                resp = await client.get(target, headers=headers)
                resp.raise_for_status()
                content = resp.content

            # 重试后仍是 HTML（非图片）→ 放弃，避免把跳转页当图片内嵌
            if _looks_like_html(content):
                agent_logger.info(f"[Graph::media] meta refresh 重试后仍为 HTML，放弃: {src[:80]}")
                return None, ""

            if len(content) > _MAX_IMAGE_BYTES:
                agent_logger.info(f"[Graph::media] 图片过大 ({len(content)}B) 跳过: {src[:80]}")
                return None, ""

            # MIME 检测
            mime = _detect_mime(resp, src)
            if not mime:
                return None, ""

            # ★ 文件特征辅助判断：正方形 + < 30KB → 极可能是 Logo/图标
            if len(content) < 30 * 1024:
                is_square = _is_square_image(content)
                if is_square:
                    src_lower = src.lower()
                    # URL 含装饰关键词 OR alt 低信息量 → 双重确认后排除
                    if any(kw in src_lower for kw in _IMG_BLOCK_URL_KW):
                        agent_logger.info(f"[Graph::media] 过滤疑似图标(SQ+<30K+URL): {src[:60]}")
                        return None, ""

            # ★ 全局 MD5 去重
            if global_seen_hashes is not None:
                img_md5 = hashlib.md5(content).hexdigest()
                count = global_seen_hashes.get(img_md5, 0) + 1
                global_seen_hashes[img_md5] = count
                if count >= 5:
                    agent_logger.info(f"[Graph::media] 全站共用图({count}次) 跳过: {src[:60]}")
                    return None, ""

            import base64
            b64 = base64.b64encode(content).decode("ascii")
            return b64, mime

    except Exception as e:
        agent_logger.info(f"[Graph::media] 下载失败: {src[:80]} | {e}")
        return None, ""


def _is_square_image(content: bytes) -> bool:
    """检测图片是否近似正方形（宽高比 0.8~1.2）"""
    try:
        import struct
        from io import BytesIO

        # 尝试 PIL
        try:
            from PIL import Image
            img = Image.open(BytesIO(content))
            w, h = img.size
            if w > 0 and h > 0:
                ratio = w / h
                return 0.8 <= ratio <= 1.2
        except Exception:
            pass

        # 回退：解析 PNG
        if content[:8] == b'\x89PNG\r\n\x1a\n':
            if len(content) >= 24:
                w = struct.unpack('>I', content[16:20])[0]
                h = struct.unpack('>I', content[20:24])[0]
                if w > 0 and h > 0:
                    return 0.8 <= w / h <= 1.2
        # 回退：解析 JPEG
        elif content[:2] == b'\xff\xd8':
            idx = 2
            while idx < min(len(content) - 8, 2048):
                marker = content[idx + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    h = struct.unpack('>H', content[idx + 5:idx + 7])[0]
                    w = struct.unpack('>H', content[idx + 7:idx + 9])[0]
                    if w > 0 and h > 0:
                        return 0.8 <= w / h <= 1.2
                    break
                idx += struct.unpack('>H', content[idx + 2:idx + 4])[0] + 2
    except Exception:
        pass
    return False


def _detect_mime(response, src: str) -> str:
    """检测图片 MIME 类型"""
    ct = response.headers.get("content-type", "")
    if ct and "image/" in ct:
        return ct.split(";")[0].strip()
    # 从扩展名推断
    ext_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp", ".svg": "image/svg+xml",
        ".bmp": "image/bmp", ".ico": "image/x-icon",
        ".avif": "image/avif",
    }
    src_lower = src.lower().split("?")[0]
    for ext, m in ext_map.items():
        if src_lower.endswith(ext):
            return m
    return ""


def _looks_like_html(content: bytes) -> bool:
    """判断下载内容是否为 HTML 跳转页而非真实图片二进制。"""
    if not content:
        return False
    head = content[:512].lstrip().lower()
    return (
        head.startswith(b"<html")
        or head.startswith(b"<!doctype")
        or b"http-equiv" in content[:200].lower()
    )


def _extract_meta_refresh_url(content: bytes) -> str:
    """从 meta refresh HTML 跳转页中解析目标 URL（可能为相对路径）。"""
    try:
        text = content.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    m = _re.search(r'url\s*=\s*["\']?\s*([^"\'>\s]+)', text, _re.I)
    if m:
        return m.group(1).strip().strip('"\'')
    return ""


async def _llm_analyze_failed_images(
    html: str, failed_urls: List[str], page_url: str
) -> Dict[str, str]:
    """
    LLM 兜底分析：下载失败的图片，请 LLM 分析是否有替代方案。

    Returns:
        {original_url: replacement_url_or_data_uri}
    """
    llm = _get_llm()
    if llm is None:
        return {}

    # 截断 HTML（只取包含失败图片的上下文）
    soup = BeautifulSoup(html, "html.parser")
    img_contexts = []
    for img in soup.find_all(list(_IMG_TAGS)):
        src = _extract_src(img)
        if src in failed_urls:
            parent = str(img.parent)[:500] if img.parent else str(img)[:500]
            img_contexts.append(parent)

    if not img_contexts:
        return {}

    prompt = (
        "你是网页图片分析专家。以下页面中某些图片无法通过常规请求下载。\n\n"
        f"页面 URL: {page_url}\n"
        f"失败的图片 URL: {', '.join(failed_urls[:5])}\n\n"
        "请分析这些图片节点的 HTML 上下文，判断：\n"
        "1. 是否有隐藏的真实图片 URL（如 data-original、CSS background-image）\n"
        "2. 是否为占位图/装饰图（可以安全忽略）\n\n"
        f"HTML 上下文:\n{'---\n'.join(img_contexts[:5])}\n\n"
        "请以 JSON 格式回复（只输出 JSON）：\n"
        '{"analysis": "分析说明", "fixes": {"失败URL": "替换URL或ignore"}}'
    )

    try:
        response = await llm.ainvoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        import json
        text = text.strip().lstrip("```json").rstrip("```").strip()
        data = json.loads(text)
        return data.get("fixes", {})
    except Exception:
        return {}


def _re_write_html_files(results: List[Dict], output_dir: str) -> None:
    """用 Base64 化后的 HTML 覆盖之前保存的 HTML 文件"""
    import re
    for row in results:
        if not row or not isinstance(row, dict):
            continue
        file_path = row.get("file_path", "")
        html = row.get("html", "")
        if not file_path or not html:
            continue
        full_path = os.path.join(output_dir, file_path)
        if os.path.exists(full_path):
            try:
                with open(full_path, "w", encoding="utf-8") as f:
                    if html.strip().startswith("<!DOCTYPE") or html.strip().startswith("<html"):
                        # 已是完整文档（模板模式），直接写入
                        f.write(html)
                    else:
                        # 旧版扁平模式，包裹 CSS
                        f.write("<!DOCTYPE html>\n<html>\n<head>\n")
                        f.write('<meta charset="utf-8">\n')
                        f.write('<meta name="viewport" content="width=device-width, initial-scale=1">\n')
                        f.write("<style>html{color-scheme:light} a,span,b,strong{color:#000!important;"
                                "text-decoration:none!important}a:hover{color:#000!important}</style>\n")
                        f.write("</head>\n<body>\n")
                        f.write(html)
                        f.write("\n</body>\n</html>")
            except Exception as e:
                agent_logger.warning(f"[Graph::media] 覆盖 HTML 失败: {full_path} | {e}")


# ============================================================================
# Node 7: 存储落盘
# ============================================================================

def _rebuild_csv_from_disk(output_dir: str, state: CrawlerState) -> List[Dict[str, str]]:
    """
    兜底函数：当 crawled_results 为空时，从磁盘上已有的 HTML 文件重建 CSV 行。

    扫描 output_dir 下所有 .html 文件，从文件名提取标题，
    从相对路径推断 ywlx（业务类型层级），读取 HTML 内容填充。
    """
    results = []
    site_name = state.get("site_name", "")
    seed_url = state.get("seed_url", "")

    if not os.path.isdir(output_dir):
        return results

    for root, dirs, files in os.walk(output_dir):
        for fname in files:
            if not fname.endswith(".html"):
                continue
            file_path = os.path.join(root, fname)
            # 跳过已由完整流程保存的路径
            if os.path.basename(file_path) == "crawl_results.csv":
                continue

            # 从文件名提取标题（去掉 .html 后缀）
            title = fname[:-5] if fname.endswith(".html") else fname

            # 从相对路径推断 ywlx（子目录层级）
            rel_dir = os.path.relpath(root, output_dir)
            if rel_dir == ".":
                nav_parts = []
            else:
                nav_parts = [p for p in rel_dir.replace("\\", "/").split("/") if p]
            padded = nav_parts + ["", "", "", ""]
            ywlx_full = "/".join(nav_parts) if nav_parts else ""

            # 读取 HTML 内容
            try:
                with open(file_path, "r", encoding="utf-8") as fp:
                    html_content = fp.read()
            except Exception:
                html_content = ""

            row = {
                "sys_platfuuid": str(uuid.uuid4()),
                "brwidcl_cpmc": site_name,
                "ywlx": ywlx_full,
                "ywlx1": padded[0],
                "ywlx2": padded[1],
                "ywlx3": padded[2],
                "ywlx4": padded[3],
                "tianextimejsj": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "title": title,
                "html": html_content,
                "download_img_url": "",
                "img_title": "",
            }
            results.append(row)

    return results


async def storage_node(state: CrawlerState) -> dict:
    """
    存储节点 — 将 crawled_results 写入标准 CSV 文件。

    职责:
      - 确保表头为标准 12 字段
      - 追加写入 CSV（文件锁保护）
      - 已有的 HTML 文件在 fetch_extract_node 中已保存
      - ★ 兜底：若 crawled_results 为空但磁盘有 HTML 文件，自动扫描重建
    """
    # ★ 优先使用 media 阶段处理后的行（含 Base64 内嵌 html）；否则回退到 fetch 累积行
    results: List[Dict] = list(state.get("media_results") or state.get("crawled_results", []))
    output_dir = state.get("output_dir", "")
    stats = state.get("stats", {})

    # ★ 按 URL 去重（保留最后一条，避免重入队 / 状态合并产生的重复行）
    _dedup: Dict[str, Dict] = {}
    for row in results:
        if not row or not isinstance(row, dict):
            continue
        _dedup[row.get("url") or row.get("title") or str(id(row))] = row
    results = list(_dedup.values())

    if not results:
        # ★ 兜底扫描：从磁盘上的 HTML 文件重建 CSV 行
        if output_dir and os.path.isdir(output_dir):
            agent_logger.info(
                f"[Graph::storage] crawled_results 为空，尝试从磁盘重建 | "
                f"output_dir={output_dir}"
            )
            results = _rebuild_csv_from_disk(output_dir, state)
            if results:
                agent_logger.info(
                    f"[Graph::storage] 磁盘重建成功 | 恢复 {len(results)} 行"
                )
            else:
                agent_logger.info("[Graph::storage] 磁盘无 HTML 文件，跳过写入")
                return {}
        else:
            agent_logger.info("[Graph::storage] 无数据需要写入")
            return {}

    csv_path = os.path.join(output_dir, "crawl_results.csv")
    agent_logger.info(f"[Graph::storage] 写入 CSV | path={csv_path} | rows={len(results)}")

    await _write_csv(csv_path, results)

    agent_logger.info(
        f"[Graph::storage] 完成 | saved={stats.get('saved', 0)} | "
        f"skipped={stats.get('skipped', 0)} | duplicate={stats.get('duplicate', 0)}"
    )
    return {}


# ============================================================================
# 条件路由函数（供 workflow.py 的 add_conditional_edges 使用）
# ============================================================================

def route_after_fetch(state: CrawlerState) -> str:
    """
    fetch_extract_node 完成后的路由:
      - queue 非空 → 继续抓取（loop back to fetch_extract_node）
      - queue 为空 → 进入评估（evaluate_node）
    """
    queue = state.get("queue", [])
    error = state.get("error", "")
    if error:
        return "storage_node"
    if queue:
        return "fetch_extract_node"
    return "evaluate_node"


def route_after_evaluate(state: CrawlerState) -> str:
    """
    evaluate_node 完成后的路由:
      - passed=True              → media_processor_node（Base64 化图片后落盘）
      - passed=False 且调整 < 3  → config_adjust_node（调整后重抓）
      - passed=False 且调整 ≥ 3 且未生成规则 → code_gen_node（LLM 最后保底）
      - passed=False 且已生成规则 → media_processor_node（不再尝试）
    """
    evaluation_dict = state.get("evaluation", {})
    adjustment_count = state.get("adjustment_count", 0)
    generation_attempted = state.get("generation_attempted", False)
    error = state.get("error", "")

    if error:
        return "storage_node"

    passed = evaluation_dict.get("passed", True)
    max_adjust = 3

    if passed:
        agent_logger.info("[Graph::route_eval] → media_processor (评估通过)")
        return "media_processor_node"

    if adjustment_count < max_adjust:
        agent_logger.info(f"[Graph::route_eval] → adjust (第 {adjustment_count + 1}/{max_adjust} 次调整)")
        return "config_adjust_node"

    # 调整已达上限，检查是否已尝试过 LLM 生成规则
    if not generation_attempted:
        agent_logger.info("[Graph::route_eval] → code_gen (传统爬虫+调整均失败，LLM 最后保底)")
        return "code_gen_node"

    agent_logger.info("[Graph::route_eval] → media_processor (已达最大调整次数且已尝试规则生成)")
    return "media_processor_node"


# ============================================================================
# CSV 行构建（保证 12 标准字段）
# ============================================================================

def _build_csv_row(page: PageData, site_name: str, nav_path: List[str]) -> Dict[str, str]:
    """从清洗后的 PageData 构建标准 CSV 行字典"""
    # nav_path → ywlx 拆分
    padded = list(nav_path) + ["", "", "", ""]
    ywlx_full = "/".join(filter(None, nav_path)) if nav_path else ""

    return {
        "sys_platfuuid": str(uuid.uuid4()),
        "brwidcl_cpmc": site_name,
        "ywlx": ywlx_full,
        "ywlx1": padded[0],
        "ywlx2": padded[1],
        "ywlx3": padded[2],
        "ywlx4": padded[3],
        "tianextimejsj": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "title": page.title or "",
        "html": page.html or "",
        "download_img_url": page.images_urls[0] if page.images_urls else "",
        "img_title": page.images_alts[0] if page.images_alts else "",
        # 内部字段（不写入 CSV 但供后续节点使用）
        "url": page.url or "",
        "file_path": "",
        "_cookies": page.extra.get("_cookies", {}),
    }


def _build_skipped_csv_row(url: str, site_name: str, nav_path: List[str],
                           title: str, reason: str, detail: str = "") -> Dict[str, str]:
    """构建跳过/失败的 CSV 行（html 字段包含原因说明）"""
    padded = list(nav_path) + ["", "", "", ""]
    ywlx_full = "/".join(filter(None, nav_path)) if nav_path else ""

    return {
        "sys_platfuuid": str(uuid.uuid4()),
        "brwidcl_cpmc": site_name,
        "ywlx": ywlx_full,
        "ywlx1": padded[0],
        "ywlx2": padded[1],
        "ywlx3": padded[2],
        "ywlx4": padded[3],
        "tianextimejsj": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "title": title or url[:60],
        "html": f"<!-- {reason}: {detail} -->",
        "download_img_url": "",
        "img_title": "",
    }


# ============================================================================
# HTML 文件落盘
# ============================================================================

# HTML 文件写入路径去重（防止同一页面被保存两次）
_saved_html_paths: Set[str] = set()
# HTML 内容去重（完整 HTML MD5 → 首次文件路径）
_saved_content_hashes: Dict[str, str] = {}
# URL 级去重（同一 URL 只保存一次，防止多路径入队导致 _1 重复）
_saved_urls: Set[str] = set()


# ============================================================================
# 固定模板 CSS — 统一排版样式
# ============================================================================

_TEMPLATE_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{max-width:900px;margin:0 auto;padding:24px 20px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;font-size:16px;line-height:1.85;color:#2c2c2c;background:#fff;-webkit-font-smoothing:antialiased}
.page-header{border-bottom:2px solid #1a73e8;padding-bottom:16px;margin-bottom:28px}
.page-header h1{font-size:26px;font-weight:700;color:#1a1a1a;line-height:1.4;margin-bottom:8px}
.page-header .breadcrumb{font-size:13px;color:#888;margin-bottom:4px}
.page-header .breadcrumb span{color:#1a73e8}
.article-meta{font-size:13px;color:#999;margin-bottom:12px}
.content h2{font-size:20px;font-weight:600;color:#1a1a1a;margin:28px 0 14px;padding-left:0}
.content h3{font-size:17px;font-weight:600;color:#333;margin:22px 0 10px}
.content h4,.content h5,.content h6{font-size:16px;font-weight:600;color:#444;margin:18px 0 8px}
.content p{margin:14px 0;text-indent:2em}
.content img{max-width:100%;height:auto;display:block;margin:16px auto;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.content div{margin:10px 0}
.content .text-center{text-align:center}
.content strong{color:#1a1a1a}
.content em{color:#555}
.page-footer{border-top:1px solid #e8e8e8;padding-top:16px;margin-top:36px;font-size:12px;color:#aaa;line-height:1.8}
.page-footer .source-info{margin-bottom:4px}
.page-footer .source-info a{color:#1a73e8;text-decoration:none}
"""

# ============================================================================
# 结构化内容构建 — 保留 DOM 层级 + 尾部噪音扫描
# ============================================================================

# 噪音正则（匹配 ≥2 个即判定为页脚噪音块）
_NOISE_PATTERNS = [
    (re.compile(r'电话[：:\s]*[\d\-]{7,}'), 'phone'),
    (re.compile(r'(服务|投诉|客服|咨询)\s*(热线|电话)[：:\s]*[\d\-]{3,}'), 'service_phone'),
    (re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'), 'email'),
    (re.compile(r'E-mail|邮箱', re.I), 'email_label'),
    (re.compile(r'地址[：:\s]*.{0,40}(路|街|号|厦|广场|区|楼|层|室|座)'), 'address'),
    (re.compile(r'版权所有|Copyright|All\s+Rights\s+Reserved', re.I), 'copyright'),
    (re.compile(r'ICP备|备案号|公网安备|ICP证'), 'icp'),
    (re.compile(r'官方微信|微信公众号|扫一扫|扫码|二维码'), 'wechat'),
    (re.compile(r'手机网站|手机版|移动版'), 'mobile'),
    (re.compile(r'技术支持'), 'tech_support'),
    (re.compile(r'传真[：:\s]*[\d\-]'), 'fax'),
    (re.compile(r'邮\s*编[：:\s]*\d{6}'), 'postal_code'),
    (re.compile(r'服务热线|投诉电话'), 'service_label'),
]

# 噪音块关键词（class/id 命中即整个块删除）
_NOISE_BLOCK_KW = re.compile(
    r'(footer|copyright|contact|bottom-info|foot|beian|banquan|sidebar_info|contact_us|left_list|left_nav)',
    re.I,
)

# 内容安全关键词（即便命中噪音模式也不删除）
_CONTENT_SAFE_KW = re.compile(
    r'(content|article|post|detail|main-text|entry|body-text|text-content|news)',
    re.I,
)


def _is_noise_block(el) -> bool:
    """判断一个元素是否为噪音块（页脚/联系方式等）"""
    if not hasattr(el, 'name'):
        return False

    # class/id 命中内容安全 → 保留
    el_attrs = el.attrs or {}
    el_class = ' '.join(el_attrs.get('class', [])).lower()
    el_id = el_attrs.get('id', '').lower()
    if _CONTENT_SAFE_KW.search(el_class) or _CONTENT_SAFE_KW.search(el_id):
        return False

    # class/id 命中噪音关键词 → 直接删除
    if _NOISE_BLOCK_KW.search(el_class) or _NOISE_BLOCK_KW.search(el_id):
        return True

    # 文本中匹配 ≥2 个噪音模式 → 噪音块
    text = el.get_text(' ', strip=True)
    if len(text) < 20:
        return False

    noise_count = sum(1 for pat, _ in _NOISE_PATTERNS if pat.search(text))
    return noise_count >= 2


def _is_likely_page_title(text: str) -> bool:
    """
    判断面包屑单元素是否像页面标题而非导航层级。
    特征: 含日期前缀(如 2019-03-28) 或 长度 > 15 字符（标题级长度）。
    这类面包屑不应该作为 nav_path 使用，会导致孤儿文件夹。
    """
    import re as _re4
    if not text:
        return False
    # 含日期前缀: "2019-03-28 xxx" 或 "2020-04-28 xxx"
    if _re4.match(r'^\d{4}[-/]\d{2}[-/]\d{2}\s', text):
        return True
    # 长度 > 15 → 很可能是文章标题而非导航标签
    if len(text) > 15:
        return True
    return False


def _should_re_enqueue(url_key: str, new_nav_path: list) -> bool:
    """
    BFS 重入队条件：当前 nav_path 比已发现的更优时，允许重新入队。
    
    条件:
      1. new_nav_path 至少 2 级（是真实类别层级，非单元素标题）
      2. new_nav_path 第一级不是页面标题（非 "2019-03-28 标题" 格式）
    """
    if not new_nav_path or len(new_nav_path) < 2:
        return False
    if _is_likely_page_title(new_nav_path[0]):
        return False
    return True


def _extract_url_path_nav(url: str) -> list:
    """
    从 URL 路径中推断导航层级（兜底方案，当 BFS nav_path 和面包屑都失败时使用）。

    例如:
      /jggs/sy88/news/company/2024/article.html → ["news", "company"]
      /a/b/c/detail.html                      → ["a", "b", "c"]

    过滤规则:
      - 去掉纯数字、日期格式、技术前缀（jggs/sy88/index/php等）
      - 去掉首页兜底词（网站地图等）
      - 最多保留 4 级
    """
    import re as _re3
    if not url:
        return []
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return []

    parts = path.split("/")
    # 去掉文件名（含扩展名的最后一段）
    if parts and "." in parts[-1]:
        parts.pop()

    cleaned = []
    skip_patterns = [
        r'^jggs$', r'^sy\d+$', r'^index$', r'^php$', r'^html$',
        r'^default$', r'^main$', r'^home$', r'^page$',
        r'^\d{4}$', r'^\d{4}-\d{2}$', r'^\d{4}-\d{2}-\d{2}$',
        r'^\d+$',
    ]
    for p in parts:
        p = p.strip().lower()
        if not p:
            continue
        if any(_re3.match(sp, p) for sp in skip_patterns):
            continue
        # 恢复原始大小写（从原始 path 中取）
        idx = parts.index(p) if p in parts else -1
        # 简单去重（相对于已有 segments）
        if p not in cleaned:
            cleaned.append(p)

    # 最多保留 4 级
    return cleaned[:4]


def _extract_breadcrumb_nav(rescued_html: str) -> list:
    """
    从 rescued HTML 中提取面包屑导航层级。

    支持两种格式：
      - <div class="breadCrumb">首页 > 走进建管 > 管理团队</div>
      - <div class="breadcrumb"><a>首页</a> > <a>走进建管</a> > ...</div>
      - 文本: "你现在的位置：网站首页 > 服务项目 > ..."

    Returns: [nav_path_parts]，剔除首页/主页等前缀，例如 ["走进建管", "管理团队"]
    """
    import re as _re2

    if not rescued_html:
        return []

    try:
        soup = BeautifulSoup(rescued_html, "html.parser")
    except Exception:
        return []

    # ── 查找面包屑容器 ──
    breadcrumb_classes = [
        'breadcrumb', 'breadCrumb', 'bread_crumb', 'crumbs', 'location',
        'position', 'site-nav', 'path-nav', 'nav-path', 'weizhi',
    ]
    bc_elements = []
    for bc_cls in breadcrumb_classes:
        found = soup.find_all(class_=_re2.compile(bc_cls, re.I))
        bc_elements.extend(found)
    # 也按 id 查找
    for el in soup.find_all(id=_re2.compile(r'(breadcrumb|position|location)', re.I)):
        if el not in bc_elements:
            bc_elements.append(el)

    for bc in bc_elements:
        text = bc.get_text(' ', strip=True)
        if not text or len(text) < 5:
            continue

        # ── 按分隔符拆 ──
        parts = _re2.split(r'\s*[>＞»→/\|]\s*', text)
        parts = [p.strip() for p in parts if p.strip()]

        # 过滤掉前缀文本（如 "你现在的位置："、"当前位置："）
        cleaned = []
        for p in parts:
            # 去掉长描述前缀
            p = _re2.sub(r'^.*[：:]\s*', '', p)
            p = p.strip()
            if not p:
                continue
            # 跳过首页/主页标记
            if _re2.match(r'^(网站)?(首页|主页|Home|HOME)$', p):
                continue
            if len(p) > 50:
                continue  # 超长文本不是导航标签
            cleaned.append(p)

        if len(cleaned) >= 1:
            return cleaned

    return []


def _build_structured_content(rescued_html: str, page_url: str = "") -> str:
    """
    从 rescued HTML 构建保留结构的清洁正文（用于填入固定模板）。

    步骤:
      1. 轻量预清理（script/style/注释/nav/header/footer 标签）
      2. 定位主内容区（_find_main_content）
      3. 内容区内: <a>→<span>、删除侧边栏块、删除噪音块
      4. 返回结构化的 body 内容 HTML
    """
    import re as _re

    if not rescued_html:
        return ""

    try:
        soup = BeautifulSoup(rescued_html, "html.parser")
    except Exception:
        return ""

    body = soup.find("body")
    if not body:
        body = soup

    # ── 1. 预清理 ──
    for tag in body.find_all(["script", "style", "noscript", "iframe", "meta", "link"]):
        tag.decompose()
    for tag in body.find_all(["header", "nav", "footer"]):
        tag.decompose()
    for comment in body.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # ── 2. 定位主内容区 ──
    content = _find_main_content(soup, page_url)
    if not content:
        # 回退：使用整个 body
        content = body

    # ── 3. 清理装饰图（底部国旗/备案图标/模板图/二维码） ──
    _DECO_IMG_PATTERNS = re.compile(
        r'(szicbok\.gif|cn\.gif|en\.gif|tubiao\.png|template/default/images/'
        r'|icons?\.|logos?\.|banner|avatar|favicon|1x1|pixel|qrcode|erweima|二维|扫码|wechat'
        r'|site\.(jpg|jpeg|png|gif))',
        re.I,
    )
    for img in list(content.find_all("img")):
        src = (img.get("src") or "").lower()
        alt = (img.get("alt") or "").lower()
        # 也检查 img 的父元素 class/id（二维码容器）
        parent = img.parent
        parent_cls = ' '.join(parent.get('class', [])).lower() if parent and hasattr(parent, 'get') else ''
        parent_id = (parent.get('id', '') or '').lower() if parent and hasattr(parent, 'get') else ''
        is_qr_container = any(kw in parent_cls or kw in parent_id
                              for kw in ('qrcode', 'erweima', '二维', '扫码', 'wechat'))
        if (_DECO_IMG_PATTERNS.search(src) or _DECO_IMG_PATTERNS.search(alt) or is_qr_container):
            parent = img.parent
            img.decompose()
            # 如果父元素变空，也删掉
            if parent and hasattr(parent, 'name') and not parent.get_text(strip=True):
                parent.decompose()

    # ── 4. 内容区内：<a> → <span> ──
    for a in list(content.find_all("a")):
        children = [c for c in a.children if not (isinstance(c, str) and not c.strip())]
        if children and all(hasattr(c, "name") and c.name == "img" for c in children):
            a.unwrap()
            continue
        span = soup.new_tag("span")
        for child in list(a.children):
            span.append(child)
        if not span.get_text(strip=True):
            span.string = a.get_text(strip=True) or ""
        a.replace_with(span)

    # ── 5. 内容区内：删除侧边栏/列表块 ──
    total_text_len = len(content.get_text(" ", strip=True))
    for el in list(content.find_all(["div", "section", "nav", "ul", "aside", "ol"])):
        el_text = len(el.get_text(" ", strip=True))
        if el_text > total_text_len * 0.35:
            continue  # 安全阀：不删正文主体
        el_children = [c for c in el.children if hasattr(c, 'name')]
        if len(el_children) < 3:
            continue
        links = len(el.find_all("a", href=True))
        ld = links / max(el_text, 1)
        el_class = ' '.join(el.attrs.get('class', [])).lower() if hasattr(el, 'attrs') else ''
        el_id = el.attrs.get('id', '').lower() if hasattr(el, 'attrs') else ''
        nav_kw = ["nav", "menu", "sidebar", "tree", "left", "right-panel", "widget", "list"]
        score = 0
        if ld > 0.5: score += 3
        elif ld > 0.3: score += 2
        if el_text < 200 and len(el_children) > 5: score += 2
        if any(kw in el_class or kw in el_id for kw in nav_kw): score += 1
        if score >= 5:
            el.decompose()

    # ── 6. 删除噪音块（页脚/联系方式等） ──
    for el in list(content.find_all(["div", "section", "p", "span"])):
        if _is_noise_block(el):
            el.decompose()

    # ── 7. 清理空容器（噪音块删除后可能留下空 div） ──
    changed = True
    while changed:
        changed = False
        for el in list(content.find_all(["div", "section"])):
            if el.find("img"):
                continue  # 有图片的容器保留
            text = el.get_text(" ", strip=True)
            if len(text) < 10 and not el.find("img"):
                el.decompose()
                changed = True

    # ── 8. 提取结构化内容 ──
    # 将所有内容子元素直接保留（h1-h6, p, div, img, etc.）
    parts = []
    for child in list(content.children):
        if hasattr(child, 'name') and child.name is not None:
            # 保留块级元素
            tag_name = child.name.lower()
            if tag_name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'div', 'img',
                            'table', 'ul', 'ol', 'blockquote', 'pre',
                            'main', 'section', 'article'):
                # 再次检查：如果是噪音块则跳过
                if _is_noise_block(child):
                    continue
                # 清理内联样式（保留必要的）
                _clean_inline_style(child)
                parts.append(str(child))
            elif tag_name == 'span':
                text = child.get_text(strip=True)
                if len(text) > 10:
                    # 长文本 span 转为 p
                    new_p = soup.new_tag("p")
                    new_p.string = text
                    parts.append(str(new_p))
            else:
                text = child.get_text(" ", strip=True)
                if len(text) > 20:
                    new_p = soup.new_tag("p")
                    new_p.string = text
                    parts.append(str(new_p))
        elif isinstance(child, str):
            text = child.strip()
            if len(text) > 15:
                new_p = soup.new_tag("p")
                new_p.string = text
                parts.append(str(new_p))

    result = "\n".join(parts)
    return result


def _clean_inline_style(el) -> None:
    """清理元素内联样式，只保留必要的 display/margin/width/text-align"""
    if not hasattr(el, 'attrs') or 'style' not in el.attrs:
        return
    style = el.get('style', '')
    allowed = []
    for part in style.split(';'):
        part = part.strip()
        if not part:
            continue
        key = part.split(':')[0].strip().lower() if ':' in part else ''
        if key in ('display', 'text-align', 'width', 'margin', 'padding'):
            allowed.append(part)
    if allowed:
        el['style'] = ';'.join(allowed)
    else:
        del el['style']


def _build_template_html(structured_body: str, title: str, nav_path: list,
                         source_url: str, crawl_time: str = "") -> str:
    """
    将结构化正文填入固定 CSS 模板，返回完整 HTML 文档。
    """
    # 面包屑
    breadcrumb_parts = []
    for i, p in enumerate(nav_path):
        if i < len(nav_path) - 1:
            breadcrumb_parts.append(f"<span>{p}</span>")
        else:
            breadcrumb_parts.append(p)
    breadcrumb_html = " &gt; ".join(breadcrumb_parts) if breadcrumb_parts else ""

    # 标题
    title_html = f"<h1>{title}</h1>" if title else ""

    # 页脚信息
    footer_parts = []
    if crawl_time:
        footer_parts.append(f"<p>爬取时间: {crawl_time}</p>")
    footer_html = "\n  ".join(footer_parts) if footer_parts else ""
    footer_block = f'<footer class="page-footer">\n  {footer_html}\n</footer>' if footer_html else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_TEMPLATE_CSS}</style>
</head>
<body>
<header class="page-header">
  <nav class="breadcrumb">{breadcrumb_html}</nav>
  {title_html}
</header>
<article class="content">
{structured_body}
</article>
{footer_block}
</body>
</html>"""


def _strip_nav_noise(html: str) -> str:
    """清洗最后一步：删除导航栏残留（面包屑 / 上下篇 / 分页控件 + 模板自带面包屑）。

    规则采用「正则关键词命中 + BeautifulSoup 整块删除」，避免纯正则在嵌套标签
    （如 <span class="fl">上一篇：<span>…</span></span>）下残留孤立闭合标签。
    命中关键词的短文本块才会被删除，正文长段落不会被误删。
    """
    if not html:
        return html

    _crumb_text = re.compile(
        r'(您的位置|您现在的位置|当前位置|您当前的位置|所在位置|你现在的位置)', re.I
    )
    _prevnext_text = re.compile(
        r'^\s*(上一篇|下一篇|上一条|下一条|返回列表|返回首页)', re.I
    )
    _pager_text = re.compile(
        r'(共\s*\d+\s*条|共\s*\d+\s*页|当前\s*\d+\s*/\s*\d+\s*页|第\s*\d+\s*/\s*\d+\s*页)'
    )
    _nav_class = re.compile(
        r'(breadcrumb|crumb|pagination|pagelist|page[-_]?list|page[-_]?index|'
        r'page[-_]?pre|page[-_]?next|page[-_]?last|page[-_]?status|'
        r'turnpage|fenye|feny|page[-_]?bar|pager)',
        re.I,
    )

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return html

    removed = 0

    # 1. 模板自带干净面包屑
    for nav in soup.find_all("nav", class_="breadcrumb"):
        nav.decompose()
        removed += 1

    # 2. class/id 命中导航关键词 → 整块删除（分页/面包屑容器）
    for el in soup.find_all():
        if getattr(el, "attrs", None) is None:
            continue
        cls = " ".join(el.get("class") or [])
        cid = el.get("id") or ""
        if _nav_class.search(cls) or _nav_class.search(cid):
            el.decompose()
            removed += 1

    # 3. 面包屑文本（位置词）→ 删除 ul/ol/div
    for el in soup.find_all(["ul", "ol", "div"]):
        t = el.get_text(" ", strip=True)
        if not t or len(t) > 80 or not _crumb_text.search(t):
            continue
        # 防护：含正文标记（标题/图片）的是内容容器，不是纯面包屑，跳过
        if el.find(["h1", "h2", "h3", "h4", "h5", "h6", "img"]):
            continue
        el.decompose()
        removed += 1

    # 4. 上一篇 / 下一篇 / 返回列表 → 删除 span/div/a/p
    for el in soup.find_all(["span", "div", "a", "p"]):
        t = el.get_text(" ", strip=True)
        if t and len(t) <= 120 and _prevnext_text.search(t):
            el.decompose()
            removed += 1

    # 5. 分页文本（共N条/当前x/y页）→ 删除 span/div
    for el in soup.find_all(["span", "div"]):
        t = el.get_text(" ", strip=True)
        if t and len(t) <= 80 and _pager_text.search(t):
            el.decompose()
            removed += 1

    # 6. 通用分隔符面包屑（无 class 的 "首页 > 栏目 > 标题" 链接组）。
    #    老站/SPA 的面包屑常是 <div><a>首页</a> > <a>栏目</a> > <span>标题</span></div>
    #    不带 breadcrumb class，上面的 class 匹配不到，只能靠结构特征识别：
    #    文本短、含"首页/Home"链接、含分隔符(>/»/→)、以站内链接为主。
    _bc_sep = re.compile(r'[>＞»→]')
    _bc_home = re.compile(r'(首页|主页|Home|HOME|网站首页)')
    for el in soup.find_all(["div", "ul", "ol", "p", "nav"]):
        t = el.get_text(" ", strip=True)
        if not t or len(t) > 100:
            continue
        # 正文特征保护：含标题/图片的容器不是纯面包屑
        if el.find(["h1", "h2", "h3", "h4", "h5", "h6", "img"]):
            continue
        links = el.find_all("a", href=True)
        if len(links) < 2:
            continue
        if not _bc_home.search(t) or not _bc_sep.search(t):
            continue
        internal = sum(
            1 for a in links
            if (a.get("href") or "").startswith("/")
            or any(d in (a.get("href") or "") for d in ("harbin-electric", "xiapuhaitou"))
        )
        if internal >= 2:
            el.decompose()
            removed += 1

    # 7. 导航链接组：站内链接 >= 5 个、文本短、无正文特征、且不是正文列表。
    #    用于清理最终模板里残留的顶部/侧边导航（如 "首页 | 公司概况 | 新闻中心..."）。
    #    安全阀：含标题/图片不删；文本 > 250 视为正文列表不删；
    #    含"条/页/日期/正文句号"等正文信号不删。
    _nav_menu = re.compile(r'首页|Home|主站|English|EN\b', re.I)
    for el in soup.find_all(["ul", "div"]):
        if el.find(["h1", "h2", "h3", "h4", "h5", "h6", "img"]):
            continue
        links = el.find_all("a", href=True)
        if len(links) < 5:
            continue
        t = el.get_text(" ", strip=True)
        if not t or len(t) > 250:
            continue
        internal = sum(
            1 for a in links
            if (a.get("href") or "").startswith("/")
            or any(d in (a.get("href") or "") for d in ("harbin-electric", "xiapuhaitou"))
        )
        if internal < 5:
            continue
        # 正文列表信号（栏目列表页的内容区通常是长文本/日期）：出现这些则视为内容。
        # 注意不能含孤立的"页/条"等字——"首页""头条"等导航词会被误伤
        if re.search(r'\d{4}[-/年]\d{1,2}|共\s*\d+\s*条|第\s*\d+\s*页|下一页|\d{1,2}[-/]\d{1,2}', t):
            continue
        # 导航菜单特征：文本里含"首页/Home"等导航标志才删。
        # 注意不能加"全短链接"分支——"新闻一/新闻二..."式列表页正文也会被误删
        if _nav_menu.search(t):
            el.decompose()
            removed += 1

    # 8. 导航容器 class/id 整块删除（menu/nav/header 关键词）。
    #    老站/SPA 的顶部主导航常是 <div class="menu"> / <td class="topnav"> /
    #    <ul id="main-menu"> 等，不含 <nav> 标签，预清理删不掉；
    #    _is_noise_block 黑名单也没有 menu/nav 关键词，只能在此最终兜底。
    #    安全阀：含 h1-h6（可能是内容标题）不删；文本>200（长内容）不删；站内链接<3（非导航）不删。
    _nav_container = re.compile(
        r'(menu|navbar|nav-bar|nav_bar|topnav|top-nav|top_nav|mainnav|main-nav|'
        r'headernav|header-nav|headmenu|head-menu|site-nav|site_nav|sitenav|'
        r'globalnav|global-nav|channel-nav|channelnav)', re.I
    )
    for el in soup.find_all(["div", "ul", "ol", "nav", "td"]):
        attrs = getattr(el, "attrs", None) or {}
        cls = " ".join(attrs.get("class") or [])
        cid = attrs.get("id") or ""
        if not (_nav_container.search(cls) or _nav_container.search(cid)):
            continue
        if el.find(["h1", "h2", "h3", "h4", "h5", "h6"]):
            continue
        t = el.get_text(" ", strip=True)
        if not t or len(t) > 200:
            continue
        links = el.find_all("a", href=True)
        # JS 模板菜单：菜单项是 span/li 模板类（navItem/navbarList/menuItem...），
        # 无 <a href> 链接（链接数=0），链接数判断失效。
        # 典型如 iYong 建站系统 <div id="menu_ver_1"> -> <li class="navItem icon-navItem">。
        # 安全阀：命中正文信号（日期/共条/页码）视为内容列表，不删。
        template_menu = bool(
            el.select("[class*=navItem], [class*=navbarList], [class*=menuItem], [class*=nav_list]")
        )
        if template_menu and not re.search(
            r'\d{4}[-/年]\d{1,2}|共\s*\d+\s*条|第\s*\d+\s*页|下一页|\d{1,2}[-/]\d{1,2}', t
        ):
            el.decompose()
            removed += 1
            continue
        if len(links) < 3:
            continue
        internal = sum(
            1 for a in links
            if (a.get("href") or "").startswith("/")
            or any(d in (a.get("href") or "") for d in ("harbin-electric", "xiapuhaitou"))
        )
        if internal >= 3:
            el.decompose()
            removed += 1

    # 8.1 iYong 建站系统页头（logo/背景图区）整块删除：
    #     <div id="head_ver_1"> / class="modulebox box_head_v1" / id="webHeaderBox"，
    #     属于站点头部导航区而非正文。
    #     安全阀：含 h1-h6、文本>200、命中正文信号（日期/共条/页码）不删。
    _head_container = re.compile(
        r'(box_head_v1|webHeaderBox|webHeader|head_ver_\d|'
        r'box_language_v1|lang_ver_\d|langlist|webLanguage)', re.I
    )
    for el in soup.find_all(["div", "section"]):
        attrs = getattr(el, "attrs", None) or {}
        cls = " ".join(attrs.get("class") or [])
        cid = attrs.get("id") or ""
        if not (_head_container.search(cls) or _head_container.search(cid)):
            continue
        if el.find(["h1", "h2", "h3", "h4", "h5", "h6"]):
            continue
        t = el.get_text(" ", strip=True)
        # 头部区通常只有 logo 图片、无文本（t 为空），属正常情况，空文本也删；
        # 仅当文本>200（疑似内容区）才放行。
        if len(t) > 200:
            continue
        if re.search(
            r'\d{4}[-/年]\d{1,2}|共\s*\d+\s*条|第\s*\d+\s*页|下一页|\d{1,2}[-/]\d{1,2}', t
        ):
            continue
        el.decompose()
        removed += 1

    # 8.2 iYong 建站系统服务栏/内嵌全站菜单残留整块删除。
    #     - <div id="service_ver_1"> 底部悬浮服务栏：结构为
    #       <li class="fitem_index"><span class="serviceItemName">首页</span> 等
    #       （首页/电话/留言/地图），class/id 命中断言，无 <a href> 链接。
    #     - <div class="menu"> 内嵌全站菜单：JS 渲染，菜单项是 span/li
    #       无 <a href>（链接数判断失效），其二级菜单容器 .sec_m/.sec_l 是
    #       iYong 独有结构签名，命中即删，避免误删普通 <div class="menu"> 正文。
    _service_bar = re.compile(r'(service_ver_\d|box_service_v1|wapService)', re.I)
    for el in soup.find_all(["div", "section"]):
        attrs = getattr(el, "attrs", None) or {}
        cls = " ".join(attrs.get("class") or [])
        cid = attrs.get("id") or ""
        if not (_service_bar.search(cls) or _service_bar.search(cid)):
            continue
        if el.find(["h1", "h2", "h3", "h4", "h5", "h6"]):
            continue
        el.decompose()
        removed += 1

    for el in soup.find_all("div", class_="menu"):
        if not (el.find(class_="sec_m") or el.find(class_="sec_l")
                or el.find(class_="first_li")):
            continue
        # 安全阀：命中正文信号（日期/共条/页码）视为内容，不删
        t = el.get_text(" ", strip=True)
        if re.search(
            r'\d{4}[-/年]\d{1,2}|共\s*\d+\s*条|第\s*\d+\s*页|下一页|\d{1,2}[-/]\d{1,2}', t
        ):
            continue
        el.decompose()
        removed += 1

    # 8.3 全站导航菜单容器（哈电 site-menu 等 Vue 模板）整块删除。
    #     菜单项是 span/li 无 <a href>（链接数判断失效），且菜单内含
    #     <h6> 英文导航标题（About us / News center...）会触发第8步的
    #     h1-h6 安全阀放行、文本又常超 200 字 → 旧规则全部漏删。
    #     安全阀：命中正文信号（日期/共条/页码）视为内容列表，不删。
    _site_nav = re.compile(
        r'(site-menu|site_nav|global-menu|global_nav|main-menu|web-menu|topNavBar)', re.I
    )
    for el in soup.find_all(["div", "nav", "header"]):
        attrs = getattr(el, "attrs", None) or {}
        cls = " ".join(attrs.get("class") or [])
        cid = attrs.get("id") or ""
        if not (_site_nav.search(cls) or _site_nav.search(cid)):
            continue
        # 结构签名：含 sub-nav 子菜单容器，或含"网站首页/首页"导航词且含 <h6> 英文标题
        t = el.get_text(" ", strip=True)
        if not (el.find(class_="sub-nav")
                or (el.find("h6") and re.search(r'网站首页|首页', t))):
            continue
        if re.search(
            r'\d{4}[-/年]\d{1,2}|共\s*\d+\s*条|第\s*\d+\s*页|下一页|\d{1,2}[-/]\d{1,2}', t
        ):
            continue
        el.decompose()
        removed += 1

    if removed:
        agent_logger.info(f"[Graph::clean] 去除导航栏残留 {removed} 处")

    return str(soup)


async def _save_html_file(page: PageData, output_dir: str) -> str:
    """保存清洗后的 HTML 到磁盘，返回相对路径。

    文件命名: nav_path 最后一级（导航栏标签），文件夹层级: nav_path 全路径。
    示例: nav_path=["工程案例","上海东湖绿地公园"] → 工程案例/上海东湖绿地公园/上海东湖绿地公园.html
    """
    import re

    # ── 完整内容 MD5 去重：同一内容不重复保存 ──
    html_md5 = hashlib.md5(
        (page.html or "").encode("utf-8", errors="replace")
    ).hexdigest()
    if html_md5 in _saved_content_hashes:
        first_path = _saved_content_hashes[html_md5]
        agent_logger.info(
            f"[Graph::save] 内容重复跳过 | md5={html_md5[:8]} | "
            f"已保存于 {first_path}"
        )
        return first_path
    _saved_content_hashes[html_md5] = ""  # 先占位，写入后更新

    # ── URL 级去重：同一 URL 只保存一次，防止多路径入队导致 _1 重复 ──
    url_key = _url_key(page.url)
    if url_key in _saved_urls:
        agent_logger.info(
            f"[Graph::save] URL 重复跳过 | {page.url[:80]}"
        )
        # 仍需更新 MD5 → path 映射以保持一致性
        return ""
    _saved_urls.add(url_key)

    # 子目录: nav_path 全路径（已通过 _sanitize_dirname 清洗）
    sub_dirs = [_sanitize_dirname(d) for d in page.nav_path if d]
    # 文件名: page.title（文章标题）→ 回退 nav_path 最后一级 → 回退 URL path
    name = page.title.strip() if page.title else ""
    if not name and page.nav_path:
        name = page.nav_path[-1].strip()
    if not name:
        parsed = urlparse(page.url)
        path = parsed.path.strip("/")
        name = path.split("/")[-1] if path else "page"
        name = re.sub(r'\.[^.]+$', '', name)
    name = re.sub(r'\s+', ' ', name)  # 换行/制表符等空白 → 单个空格（防止文件名含 \r\n 导致 Invalid argument）
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip("._ ")[:60] or "page"

    dir_path = os.path.join(output_dir, *sub_dirs) if sub_dirs else os.path.join(output_dir, "未分类")
    os.makedirs(dir_path, exist_ok=True)

    file_path = os.path.join(dir_path, f"{name}.html")
    # 冲突处理
    counter = 1
    while os.path.exists(file_path):
        file_path = os.path.join(dir_path, f"{name}_{counter}.html")
        counter += 1

    # ── 路径级去重：同一路径只写一次（防止 LangGraph 状态循环导致重复调用） ──
    norm_path = os.path.normpath(file_path)
    if norm_path in _saved_html_paths:
        agent_logger.info(f"[Graph::save] 跳过重复文件: {norm_path}")
        return os.path.relpath(file_path, output_dir)
    _saved_html_paths.add(norm_path)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _write_html_file, file_path, page.html)
    # 更新内容哈希映射为实际文件路径
    _saved_content_hashes[html_md5] = os.path.relpath(file_path, output_dir)

    return os.path.relpath(file_path, output_dir)


def _write_html_file(path: str, html: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        if html.strip().startswith("<!DOCTYPE") or html.strip().startswith("<html"):
            # 已是完整文档（模板模式），直接写入
            f.write(html)
        else:
            # 旧版扁平模式，包裹 CSS
            f.write("<!DOCTYPE html>\n<html>\n<head>\n")
            f.write('<meta charset="utf-8">\n')
            f.write('<meta name="viewport" content="width=device-width, initial-scale=1">\n')
            f.write("<style>html{color-scheme:light} a,span,b,strong{color:#000!important;"
                    "text-decoration:none!important}a:hover{color:#000!important}</style>\n")
            f.write("</head>\n<body>\n")
            f.write(html)
            f.write("\n</body>\n</html>")


def _sanitize_dirname(name: str) -> str:
    import re
    name = re.sub(r'\s+', ' ', name)  # 换行/制表符等空白 → 单个空格
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip("._ ")[:40]
    return name or "未分类"


# ============================================================================
# CSV 写入
# ============================================================================

async def _write_csv(csv_path: str, rows: List[Dict[str, str]]) -> None:
    """写入 CSV（带 BOM，文件锁保护）"""
    import csv
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _write_csv_sync, csv_path, rows)


def _write_csv_sync(csv_path: str, rows: List[Dict[str, str]]) -> None:
    import csv
    # ★ 覆盖写（w 模式）：storage 是最终落盘节点，append 会把多次运行的旧行累积进 CSV
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ============================================================================
# LLM 评估实现
# ============================================================================

async def _llm_evaluate(llm, results: List[Dict], stats: Dict,
                        site_name: str, seed_url: str) -> EvaluationResult:
    """使用 LLM 评估爬取结果质量"""

    # 构建评估上下文（摘要，不发送完整 HTML）
    saved_count = stats.get("saved", 0)
    failed_count = stats.get("failed", 0)
    skipped_count = stats.get("skipped", 0)
    dup_count = stats.get("duplicate", 0)

    # 对前 10 条结果提取摘要
    page_summaries = []
    for i, row in enumerate(results[:10]):
        html = row.get("html", "")
        text_len = len(BeautifulSoup(html, "html.parser").get_text(strip=True)) if html else 0
        has_img = bool(row.get("download_img_url", ""))
        page_summaries.append(
            f"  {i+1}. title={row.get('title', '')[:40]} | "
            f"text_len={text_len} | has_img={has_img} | "
            f"ywlx={row.get('ywlx', '')}"
        )

    context = (
        f"站点: {site_name} ({seed_url})\n"
        f"统计: 保存={saved_count}, 失败={failed_count}, 跳过={skipped_count}, 重复={dup_count}\n"
        f"前 {len(page_summaries)} 条结果摘要:\n" + "\n".join(page_summaries)
    )

    prompt = f"""你是一个爬虫质量评估专家。请评估以下爬取结果的质量。

{context}

请以 JSON 格式返回（不要任何其他文字）:
{{
  "passed": true或false,
  "score": 0.0到1.0的质量评分,
  "summary": "一句话总结",
  "suggestion": "如果未通过，建议的修复措施",
  "issues": [
    {{"type": "anti_crawl|content_quality|image_missing|coverage|other",
      "severity": "info|warning|critical",
      "description": "问题描述",
      "affected_pages": 受影响页面数}}
  ],
  "needs_js_render": true或false,
  "recommended_ua": "如果需要换UA，在此填写；否则空字符串",
  "recommended_headers": {{"Header-Name": "value"}}
}}

评估标准:
- 如果 saved≥3 且失败率<30% 且大部分页面text_len>500 → passed=true, score≥0.8
- 如果大量页面为404/空 → 可能是列表页误判或反爬 → needs_js_render=true
- 如果有图片但download_img_url为空 → image_missing问题
- 如果所有页面都是同一内容 → 可能是反爬返回了统一样式页"""

    try:
        response = await llm.ainvoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        # 清理 markdown 代码块标记
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[:-3].strip()
            if text.startswith("json"):
                text = text[4:].strip()

        import json
        data = json.loads(text)
        return EvaluationResult(**data)
    except Exception as e:
        agent_logger.warning(f"[Graph::evaluate] LLM JSON 解析失败: {e}")
        raise


def _heuristic_evaluate(results: List[Dict], stats: Dict) -> EvaluationResult:
    """
    无 LLM 时的纯启发式评估（降级方案）。

    规则:
      - saved >= 3 且失败率 < 50% → passed
      - 否则根据统计信息给出建议
    """
    saved = stats.get("saved", 0)
    failed = stats.get("failed", 0)
    skipped = stats.get("skipped", 0)
    total = saved + failed + skipped
    failure_rate = failed / max(total, 1)

    issues = []
    passed = True
    score = 0.9

    if saved == 0:
        passed = False
        score = 0.1
        issues.append(QualityIssue(
            type="content_quality", severity="critical",
            description="无任何有效页面被保存", affected_pages=total,
        ))
    elif failure_rate > 0.5:
        passed = False
        score = 0.4
        issues.append(QualityIssue(
            type="anti_crawl", severity="warning",
            description=f"失败率过高 ({failure_rate:.0%})，可能存在反爬", affected_pages=failed,
        ))

    # 检查图片
    pages_with_images = sum(1 for r in results if r.get("download_img_url", ""))
    if saved > 0 and pages_with_images == 0:
        issues.append(QualityIssue(
            type="image_missing", severity="info",
            description="所有页面均无图片", affected_pages=saved,
        ))

    suggestion = ""
    if not passed:
        if failure_rate > 0.5:
            suggestion = "建议启用 JS 渲染 (Playwright) 或更换 User-Agent"
        elif saved == 0:
            suggestion = "目标站点可能为 SPA/强 JS 页面，需要启用 Playwright"

    return EvaluationResult(
        passed=passed, score=score,
        issues=issues,
        summary=f"启发式评估: saved={saved}, failed={failed}, skipped={skipped}",
        suggestion=suggestion,
        needs_js_render=(failure_rate > 0.5 or saved == 0),
    )


# ============================================================================
# Node 7: LLM 生成提取规则（最后保底，仅在传统爬虫 + 配置调整全部失败后调用）
# ============================================================================

async def code_gen_node(state: CrawlerState) -> dict:
    """
    LLM 代码生成节点 — 分析失败的页面，生成站点专有提取规则。

    触发条件:
      - 传统爬虫 (fetch_extract_node) 结果不理想
      - config_adjust_node 调整已达上限（3 轮）
      - generation_attempted 为 False（防止死循环）

    输出:
      - extraction_rules: 结构化规则集（不是 Python 代码，是 CSS 选择器配置）
      - generation_attempted = True
      - 清空队列，准备用新规则重新抓取
    """
    results: List[Dict] = list(state.get("crawled_results", []))
    seed_url = state.get("seed_url", "")
    evaluation_dict = state.get("evaluation", {})
    site_name = state.get("site_name", "")
    stats = state.get("stats", {})
    current_html = state.get("current_html", "")

    generation_attempted = state.get("generation_attempted", False)

    if generation_attempted:
        agent_logger.info("[Graph::code_gen] 已生成过规则，不再重复")
        return {"generation_attempted": True}

    # ── 收集失败页面的 HTML 样本 ──
    samples = _collect_failed_samples(results, current_html)
    if not samples:
        agent_logger.warning("[Graph::code_gen] 无有效 HTML 样本，跳过")
        return {"generation_attempted": True, "error": "无 HTML 样本"}

    llm = _get_llm()
    if llm is None:
        agent_logger.warning("[Graph::code_gen] 无 LLM 可用，无法生成规则")
        return {"generation_attempted": True, "error": "LLM 不可用"}

    # ── 调用 LLM 分析页面结构 ──
    try:
        rules = await _llm_generate_rules(llm, samples, seed_url, site_name, stats, evaluation_dict)
    except Exception as e:
        agent_logger.error(f"[Graph::code_gen] LLM 生成规则失败: {e}")
        return {"generation_attempted": True, "error": str(e)}

    # ── 安全校验 ──
    if not rules or not rules.content_selectors:
        agent_logger.warning("[Graph::code_gen] LLM 未返回有效规则")
        return {"generation_attempted": True, "error": "LLM 返回空规则"}

    validation_ok, vmsg = _validate_rules(rules)
    if not validation_ok:
        agent_logger.error(f"[Graph::code_gen] 规则安全校验失败: {vmsg}")
        return {"generation_attempted": True, "error": f"规则校验失败: {vmsg}"}

    agent_logger.info(
        f"[Graph::code_gen] 规则生成成功 | "
        f"content={len(rules.content_selectors)} title={len(rules.title_selectors)} "
        f"images={len(rules.image_selectors)} remove={len(rules.remove_selectors)} | "
        f"confidence={rules.confidence:.2f}"
    )
    agent_logger.info(f"[Graph::code_gen] 规则总结: {rules.summary}")

    # ── 重建队列，用新规则重新抓取 ──
    queue: List[Dict] = [{
        "url": seed_url,
        "depth": 1,
        "nav_path": [],
        "is_homepage": True,
    }]

    return {
        "extraction_rules": rules.model_dump(),
        "generation_attempted": True,
        "queue": queue,
        "seen_url_keys": [_url_key(seed_url)],
        "crawled_results": [],
        "crawler_config": state.get("crawler_config", {}),  # 保留配置调整
    }


# ============================================================================
# LLM 生成规则 — 核心实现
# ============================================================================

def _collect_failed_samples(results: List[Dict], current_html: str) -> List[str]:
    """
    收集失败/低质量页面的 HTML 样本（截断至 4KB 避免超出 token 限制）。
    优先从 crawled_results 取，若无则用 current_html。
    """
    samples: List[str] = []
    for row in results:
        html = row.get("html", "")
        if html and len(html) < 200:  # 短 HTML = 提取失败的可能性大
            samples.append(html[:3000])
        elif html:
            # 也收集一些正常 HTML 作为参考
            if len(samples) < 3:
                samples.append(html[:4000])

    # 如果没有 crawled_results 中的 HTML，用 current_html
    if not samples and current_html:
        samples.append(current_html[:4000])

    # 限制样本数
    return samples[:5]


async def _llm_generate_rules(
    llm, samples: List[str], seed_url: str,
    site_name: str, stats: Dict, evaluation_dict: Dict
) -> Optional[ExtractionRules]:
    """调用 LLM 分析页面 HTML 结构，生成定制提取规则"""

    eval_summary = evaluation_dict.get("summary", "")
    eval_issues = evaluation_dict.get("issues", [])

    issues_text = "\n".join(
        f"  - [{iss.get('type')}] {iss.get('description', '')}"
        for iss in eval_issues
    ) if eval_issues else "（无具体问题）"

    # 截断样本
    truncated = "\n\n---\n\n".join(
        f"[样本 {i+1}]\n{s[:3000]}" for i, s in enumerate(samples[:3])
    )

    prompt = f"""你是一个网页结构分析专家。传统爬虫在抓取以下网站时失败，需要你分析页面 HTML 结构，生成定制的 CSS 选择器规则。

站点: {site_name} ({seed_url})
当前统计: 保存={stats.get('saved',0)}, 失败={stats.get('failed',0)}, 跳过={stats.get('skipped',0)}
评估问题:
{issues_text}
评估摘要: {eval_summary}

以下是该网站页面的 HTML 片段（已截断）:

{truncated}

请分析这个网站的内容结构，以 JSON 格式返回提取规则（严格 JSON，不要任何其他文字）:

{{
  "content_selectors": [
    {{"selector": "div.article-body", "purpose": "content", "note": "正文容器"}}
  ],
  "title_selectors": [
    {{"selector": "h1.entry-title", "purpose": "title", "note": "文章标题"}}
  ],
  "image_selectors": [
    {{"selector": "div.article-body img", "purpose": "image", "note": "正文图片"}}
  ],
  "remove_selectors": [
    {{"selector": "div.advertisement", "purpose": "remove", "note": "广告区域"}},
    {{"selector": "div.sidebar", "purpose": "remove", "note": "侧边栏"}}
  ],
  "summary": "该网站使用 WordPress，正文在 article.post 中，标题为 h1.entry-title",
  "confidence": 0.8
}}

规则:
- content_selectors: 正文容器（按优先级排列，第一个匹配到就停止）
- title_selectors: 标题选择器（同理，按优先级）
- image_selectors: 正文图片选择器（用于提取 img 标签）
- remove_selectors: 必须移除的噪音元素
- summary: 用中文一句话描述站点结构
- confidence: 0-1，你对自己分析的信心

只使用标准 CSS 选择器（标签名、class、id）。不要包含 JavaScript、XPath、或其他非 CSS 选择器。"""

    response = await llm.ainvoke(prompt)
    text = response.content if hasattr(response, "content") else str(response)

    # 清理 markdown 代码块
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3].strip()
        if text.startswith("json"):
            text = text[4:].strip()

    import json
    data = json.loads(text)
    return ExtractionRules(**data)


# ============================================================================
# 安全校验
# ============================================================================

# 禁止的选择器模式（防注入/防任意操作）
_FORBIDDEN_PATTERNS = [
    "javascript:", "<script", "</script", "onerror=", "onload=",
    "exec(", "eval(", "__import__", "os.", "subprocess",
    "open(", "write(", "remove(", "rm ", "delete ",
    "file://", "etc/passwd", "C:\\", "\\\\",
]

# 允许的用途值
_ALLOWED_PURPOSES = {"content", "title", "image", "remove"}


def _validate_rules(rules: ExtractionRules) -> tuple:
    """
    安全校验 LLM 生成的提取规则。

    校验项:
      1. 所有选择器是字符串、非空
      2. 不含禁止的关键词（防注入）
      3. purpose 在允许范围内
      4. 移除选择器不会删除 body/html

    Returns:
        (passed: bool, reason: str)
    """
    all_rules: List[GeneratedRule] = []
    all_rules.extend(rules.content_selectors)
    all_rules.extend(rules.title_selectors)
    all_rules.extend(rules.image_selectors)
    all_rules.extend(rules.remove_selectors)

    if not all_rules:
        return False, "规则为空"

    for rule in all_rules:
        sel = rule.selector
        purpose = rule.purpose

        # 1. 非空
        if not sel or not isinstance(sel, str):
            return False, f"选择器为空或非字符串: {sel}"

        # 2. 长度合理
        if len(sel) > 500:
            return False, f"选择器过长 ({len(sel)}字符)"

        # 3. 不含禁止词
        sel_lower = sel.lower()
        for fp in _FORBIDDEN_PATTERNS:
            if fp.lower() in sel_lower:
                return False, f"选择器含禁止模式 '{fp}': {sel}"

        # 4. purpose 合法
        if purpose not in _ALLOWED_PURPOSES:
            return False, f"非法的 purpose: '{purpose}'"

        # 5. remove 规则不能针对 body/html
        if purpose == "remove":
            if sel_lower.strip() in ("body", "html", "head", "html body"):
                return False, f"禁止移除 {sel} 元素"

    return True, "ok"


# ============================================================================
# 应用 LLM 生成的规则提取内容
# ============================================================================

def _extract_with_rules(html: str, page_url: str, rules_dict: Dict) -> Tuple[str, str, int]:
    """
    使用 LLM 生成的规则提取页面内容。

    代替默认的 trafilatura → BS4 管道。

    Args:
        html:       原始 HTML
        page_url:   页面 URL（用于图片路径绝对化）
        rules_dict: ExtractionRules 序列化

    Returns:
        (cleaned_html, text_content, images_count)
    """
    if not rules_dict or not html:
        return html, "", 0

    try:
        rules = ExtractionRules(**rules_dict)
    except Exception:
        return html, "", 0

    soup = BeautifulSoup(html, "html.parser")

    # ── 1. 移除噪音 ──
    for tag in soup.find_all(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
        tag.decompose()
    for rule in rules.remove_selectors:
        try:
            for el in soup.select(rule.selector):
                el.decompose()
        except Exception:
            pass

    # ── 2. 定位正文容器 ──
    content = None
    for rule in rules.content_selectors:
        try:
            found = soup.select_one(rule.selector)
            if found and len(found.get_text(strip=True)) > 50:
                content = found
                break
        except Exception:
            continue

    if not content:
        body = soup.find("body")
        content = body if body else soup

    # ── 3. 修正图片路径 ──
    for img in content.find_all("img") if content else []:
        src = img.get("src", "")
        if src and not src.startswith(("http://", "https://", "data:")):
            img["src"] = urljoin(page_url, src)

    # ── 4. 提取标题（优先用规则，降级用 <title>） ──
    title = ""
    for rule in rules.title_selectors:
        try:
            found = soup.select_one(rule.selector)
            if found:
                title = found.get_text(strip=True)
                break
        except Exception:
            continue

    # ── 5. 统计图片 ──
    images = []
    for rule in rules.image_selectors:
        try:
            for img in soup.select(rule.selector):
                src = img.get("src", "")
                if src:
                    images.append(src)
        except Exception:
            continue
    # 如果规则没找到图，降级到 content 内全局搜索
    if not images and content:
        for img in content.find_all("img"):
            src = img.get("src", "")
            if src:
                images.append(src)

    text = content.get_text(" ", strip=True) if content else ""
    cleaned_html = str(content) if content else html

    return cleaned_html, text or "", len(images)
