"""
全站递归爬取工作流节点函数 (Phase 1 企业级升级)

1. full_site_fetch_node  — BFS 页面抓取 + 链接发现（tenacity 重试 + 结构化日志）
2. content_clean_v2_node — 内容清洗 + Pydantic 数据校验 + 结构化日志
3. multi_level_store_node — 多级文件夹存储 + extracted_data 输出
4. fallback_node          — 致命错误兜底节点

关键特性：
- Partial Update 状态返回（只返回变更字段）
- tenacity 指数退避重试（网络超时/5xx）
- Pydantic NewsArticleSchema 数据校验
- 结构化 agent_logger 日志（兼容 LangSmith/Langfuse）
"""

import os
import re
import time
import random
import hashlib
import logging
import traceback
import requests
import urllib3
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
from typing import Dict, Any, List, Optional, Set, Tuple, Callable
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
from functools import wraps

# Phase 1: tenacity 重试库
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

# Phase 1: Pydantic 校验 + 结构化日志
from schemas import (
    AgentState,
    NewsArticleSchema,
    ValidationResult,
    agent_logger,
)

import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== Phase 1: 重试判断函数 ====================

def _is_retryable_error(exception: Exception) -> bool:
    """判断异常是否为可重试类型（网络超时、5xx）"""
    if isinstance(exception, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exception, requests.HTTPError):
        if hasattr(exception, "response") and exception.response is not None:
            status = exception.response.status_code
            return status >= 500  # 5xx 可重试
        return False
    return False


def _is_fatal_error(exception: Exception) -> bool:
    """判断异常是否为不可重试的确定性错误（403/404）"""
    if isinstance(exception, requests.HTTPError):
        if hasattr(exception, "response") and exception.response is not None:
            status = exception.response.status_code
            return 400 <= status < 500 and status not in (429,)
    return False


# ==================== 常量 ====================

# ★ 真实浏览器 User-Agent 池（防 WAF 拦截）
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

_HEADERS = {
    "User-Agent": config.USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.3",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


def _get_robust_session():
    """创建一个带自动重试和超时机制的 requests Session"""
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount('http://', HTTPAdapter(max_retries=retries))
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session

_UNIFIED_CSS = """
/* ===== 全局重置：强制覆盖原站样式，防止内容重叠 ===== */
* {
    position: static !important;
    float: none !important;
    left: auto !important;
    right: auto !important;
    top: auto !important;
    bottom: auto !important;
}
html, body {
    margin: 0;
    padding: 0;
}
body {
    font-family: "Microsoft YaHei", "PingFang SC", "Helvetica Neue", sans-serif;
    color: #333;
    line-height: 1.8;
    background: #fff;
    padding: 20px;
}
.content-wrapper {
    max-width: 860px;
    margin: 0 auto;
    word-wrap: break-word;
    overflow-wrap: break-word;
}
h1 { text-align: center; font-size: 24px; margin: 20px 0 30px 0; }
h2 { font-size: 20px; margin: 20px 0 12px 0; }
h3 { font-size: 18px; margin: 16px 0 10px 0; }
h4, h5, h6 { font-size: 16px; margin: 12px 0 8px 0; }
.publish-time { color: #999; font-size: 14px; text-align: center; margin-bottom: 30px; }
p { font-size: 16px; line-height: 2; text-indent: 2em; margin: 8px 0; }
p:has(img) { text-indent: 0; }
img {
    max-width: 100% !important;
    height: auto !important;
    display: block !important;
    margin: 12px auto !important;
}
video { max-width: 100%; display: block; margin: 12px auto; }
ul, ol { font-size: 16px; line-height: 2; margin: 8px 0; padding-left: 2em; }
li { margin-bottom: 5px; }
table { max-width: 100%; border-collapse: collapse; margin: 12px auto; }
table td, table th { border: 1px solid #ddd; padding: 8px; font-size: 14px; }
a { color: #1a73e8; text-decoration: underline; word-break: break-all; }
/* 强制覆盖原站 flex 布局 */
.row, [class*="col-"], .flex, [class*="flex"] {
    display: block !important;
    flex: none !important;
    flex-wrap: nowrap !important;
}
@media (max-width: 767px) { body { padding: 10px; } }
"""

# 头部排除特征
_HEADER_PATTERNS = ["nav", "menu", "header", "top-bar", "topbar", "head-",
                    "headfull", "headpublic", "webnav", "absolutemodule",
                    "navinner", "ncenter", "nmain"]

# 页脚排除特征
_FOOTER_TEXT_PATTERNS = [
    r"版权", r"Copyright", r"©", r"ICP备", r"ICP证",
    r"备案号", r"公网安备", r"技术支持", r"Powered.by",
    r"All Rights Reserved",
]

# URL 后缀排除
_SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
                    ".css", ".js", ".json", ".xml", ".pdf", ".doc", ".docx",
                    ".xls", ".xlsx", ".zip", ".rar", ".mp3", ".mp4", ".avi")

_SKIP_SCHEMES = ("mailto:", "javascript:", "tel:", "ftp:")

# ★ 域名到公司名的映射（用于生成最外层根文件夹）
DOMAIN_TO_COMPANY = {
    # 龙源电力相关
    'lywpower': '龙源电力',
    'longyuan': '龙源电力',
    'clypg': '龙源电力',
    # 国家能源集团相关
    'chnenergy': '国家能源集团',
    'ceic': '国家能源集团',
    # 汇能集团相关
    'huinenggroup': '汇能集团',
    'huineng': '汇能集团',
}


def get_current_company(url):
    """
    根据域名自动识别公司名。
    
    优先级：
    1. 映射表精确匹配 → 返回中文公司名
    2. 域名主体含中文 → 直接使用
    3. 域名主体为纯英文且不在映射表 → 使用 域名_时间戳 防止多网站覆盖
    """
    from datetime import datetime
    
    domain = urlparse(url).netloc.lower().replace('www.', '')  # 强制去除 www.
    
    # 1. 优先匹配映射表
    for key, company in DOMAIN_TO_COMPANY.items():
        if key in domain:
            return company
            
    # 2. 提取域名主体
    parts = domain.split('.')
    domain_body = parts[0] if parts else "unknown"
    
    # 3. 如果域名主体含中文 → 直接使用
    if not domain_body.isascii():
        return domain_body
    
    # 4. 纯英文域名兜底：使用 "域名_时间戳" 防止不同网站数据互相覆盖
    #    格式示例: example_com_20260101_120000
    safe_domain = domain.replace(':', '_').replace('.', '_')
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fallback_name = f"{safe_domain}_{timestamp}"
    print(f"⚠️ [警告] 无法识别域名 {domain}，使用兜底命名: {fallback_name}")
    return fallback_name


def clean_filename(name):
    """强制清洗文件名，确保合法且易读"""
    if not name:
        return "未命名"
    # 1. 移除 Windows 非法字符
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', '', name)
    # 2. 移除不可见字符和多余空白
    name = re.sub(r'\s+', ' ', name).strip()
    # 3. 截断过长的文件名（保留前 40 个字符，防止路径过长报错）
    if len(name) > 40:
        name = name[:40]
    # 4. 如果清洗后为空，返回默认值
    return name if name else "未命名"


# ==================== 内容质量过滤器 ====================

def _check_content_quality(html: str, url: str) -> bool:
    """
    检查清洗后的 HTML 内容质量。
    提取可见文本并计算字符数，低于阈值时返回 False。

    Args:
        html: 清洗后的 HTML 内容
        url:  页面 URL（用于日志）

    Returns:
        True  = 内容质量合格，可以保存
        False = 内容过少，应跳过
    """
    if not config.CONTENT_QUALITY_FILTER_ENABLED:
        return True
    min_chars = config.CONTENT_QUALITY_MIN_CHARS
    if min_chars <= 0:
        return True

    try:
        soup = BeautifulSoup(html, "html.parser")
        # 去除 script/style 标签后取纯文本
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        # 统计有效字数（排除纯空白）
        char_count = len(text)
        if char_count < min_chars:
            print(f"  ⚠️ [质量过滤] 跳过页面 {url[:100]}，原因：提取内容过少 ({char_count} 字符，阈值 {min_chars})")
            return False
    except Exception as e:
        print(f"  ⚠️ [质量过滤] 检查异常，放行: {e}")
        return True
    return True

# ==================== 节点1: BFS 页面抓取 + 链接发现 ====================

def full_site_fetch_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    从 url_queue 弹出一个 URL 进行抓取，发现该页面所有同域链接加入队列。
    状态字段操作：url_queue[0] → pop → fetch → 发现新链接 → 加入 url_queue → current_page

    首页（depth=1, queue首个）额外执行：
      - 提取一级导航映射 nav_mapping 存入 state
    """
    if not state.get("url_queue"):
        return {**state, "current_page": {}, "error": ""}

    # 弹出队列头部
    queue = list(state["url_queue"])
    item = queue.pop(0)
    url = item["url"]
    depth = item.get("depth", 1)
    # ★ current_depth 从队列 item 中取（每个页面记录自己的导航深度）
    current_depth = item.get("nav_depth", 1)
    parent_breadcrumb = item.get("breadcrumb", [])
    parent_url = item.get("parent_url", "")
    is_homepage = (current_depth == 1)

    visited = list(state.get("visited", []))
    results = list(state.get("results", []))
    nav_mapping = dict(state.get("nav_mapping", {}))
    max_nav_depth = state.get("max_nav_depth", 4)

    print(f"\n{'='*60}")
    print(f"[抓取] URL深度={depth} 导航级={current_depth}/{max_nav_depth} 队列剩余={len(queue)} URL={url[:100]}")
    print(f"{'='*60}")

    # 🔴 深度拦截：如果当前导航深度超过最大允许级别，跳过
    if current_depth > max_nav_depth:
        print(f"  ⛔ [深度拦截] 导航深度 {current_depth} > {max_nav_depth}，停止深入，丢弃此URL")
        stats = dict(state.get("stats", {"total": 0, "success": 0, "failed": 0, "skipped": 0}))
        stats["skipped"] += 1
        return {
            **state,
            "url_queue": queue,
            "visited": visited,
            "results": results,
            "stats": stats,
            "current_page": {},
            "error": "",
            "nav_mapping": nav_mapping,
            "current_depth": current_depth,
        }

    fetch_start = time.time()
    raw_html = None
    http_status = 0
    error_log = list(state.get("error_log", []))
    failures = dict(state.get("node_consecutive_failures", {}))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        retry=retry_if_exception_type(
            (requests.Timeout, requests.ConnectionError, requests.HTTPError)
        ),
        before_sleep=before_sleep_log(agent_logger, logging.WARNING),
        reraise=True,
    )
    def _do_fetch(target_url: str) -> requests.Response:
        """tenacity 包装的抓取逻辑：网络错误/5xx 指数退避重试最多3次，403/404不重试"""
        session = _get_robust_session()
        parsed_url = urlparse(target_url)
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Referer": parsed_url._replace(path='/', query='', fragment='').geturl(),
        }
        resp = session.get(target_url, headers=headers, timeout=15, verify=False)
        # 403/404 返回原始响应不抛异常（不重试），但调用方会检查 status
        if 400 <= resp.status_code < 500 and resp.status_code not in (429,):
            return resp  # 确定性错误，不触发重试
        # 429 触发重试
        if resp.status_code == 429:
            raise requests.HTTPError(f"429 Too Many Requests", response=resp)
        resp.raise_for_status()  # 5xx 触发重试
        return resp

    # ===== 执行抓取（首页先用 Playwright）=====
    if is_homepage:
        agent_logger.info(f"full_site_fetch | 首页抓取 | url={url[:100]} | 使用Playwright渲染")
        raw_html = _fetch_with_playwright(url)

    if not raw_html:
        try:
            agent_logger.info(f"full_site_fetch | 静态抓取 | url={url[:100]}")
            resp = _do_fetch(url)
            http_status = resp.status_code
            if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "latin-1"):
                resp.encoding = resp.apparent_encoding or "utf-8"
            raw_html = resp.text
        except Exception as e:
            http_status = getattr(e, 'response', None)
            if http_status and hasattr(http_status, 'status_code'):
                http_status = http_status.status_code
            else:
                http_status = 0

            # 记录错误日志
            error_type = "HTTP_CLIENT_ERROR" if _is_fatal_error(e) else "NETWORK_TIMEOUT_5XX"
            error_log.append({
                "timestamp": datetime.now().isoformat(),
                "node": "full_site_fetch",
                "url": url[:100],
                "error_type": error_type,
                "message": f"{type(e).__name__}: {str(e)[:200]}",
            })
            # 递增连续失败计数
            count = failures.get("full_site_fetch", 0) + 1
            failures["full_site_fetch"] = count
            agent_logger.warning(
                f"full_site_fetch | 抓取失败 (连续 {count}/3) | url={url[:80]} | "
                f"error={type(e).__name__} | http_status={http_status}"
            )
            # 连续失败 ≥3 → fatal_error
            if count >= 3:
                agent_logger.error(f"full_site_fetch | 连续失败 {count} 次，触发 fatal_error")
                return {
                    "url_queue": queue,
                    "visited": visited,
                    "results": results,
                    "current_page": {},
                    "error": "",
                    "error_log": error_log,
                    "node_consecutive_failures": failures,
                    "fatal_error": True,
                    "current_url": url,
                }

    if not raw_html:
        stats = dict(state.get("stats", {"total": 0, "success": 0, "failed": 0, "skipped": 0}))
        stats["failed"] += 1
        elapsed = time.time() - fetch_start
        agent_logger.warning(
            f"full_site_fetch | 无内容返回 | url={url[:80]} | "
            f"耗时={elapsed:.1f}s | http_status={http_status}"
        )
        return {
            "url_queue": queue,
            "visited": visited,
            "results": results,
            "stats": stats,
            "current_page": {},
            "error": "",
            "error_log": error_log,
            "node_consecutive_failures": failures,
            "current_url": url,
        }

    # 成功 → 重置失败计数
    if failures.get("full_site_fetch", 0) > 0:
        failures["full_site_fetch"] = 0

    elapsed = time.time() - fetch_start
    agent_logger.info(
        f"full_site_fetch | 抓取成功 | url={url[:80]} | "
        f"耗时={elapsed:.1f}s | http_status={http_status} | "
        f"content_len={len(raw_html)}"
    )

    soup = BeautifulSoup(raw_html, "html.parser")

    # ★ 首页：提取一级导航映射
    if is_homepage and not nav_mapping:
        nav_mapping = _extract_nav_mapping(soup, state["base_url"])
        if nav_mapping:
            print(f"  📋 [导航映射] 共提取 {len(nav_mapping)} 个一级导航:")
            for name, prefix in nav_mapping.items():
                print(f"      {name} → {prefix}")

    # 提取面包屑
    breadcrumb = _parse_breadcrumb(soup)
    if not breadcrumb:
        breadcrumb = parent_breadcrumb

    # 提取页面标题
    title = _extract_page_title(soup)

    # 发现新链接（仅当导航深度未达上限时才继续深入）
    if current_depth < max_nav_depth:
        new_links = _extract_same_domain_links(soup, url, state["base_url"])
        next_nav_depth = current_depth + 1
        for link_url, link_text in new_links:
            norm = _normalize_url(link_url)
            if norm not in visited:
                visited.append(norm)
                queue.append({
                    "url": link_url,
                    "depth": depth + 1,
                    "nav_depth": next_nav_depth,
                    "breadcrumb": breadcrumb + [link_text] if link_text else breadcrumb,
                    "parent_url": url,
                })
                if len(visited) >= config.MAX_PAGES:
                    break
    else:
        print(f"  ⏹ [深度已达上限] 导航级={current_depth}/{max_nav_depth}，不再发现子链接")

    # 设置当前页面交给清洗节点
    current_page = {
        "url": url,
        "depth": depth,
        "title": title,
        "breadcrumb": breadcrumb,
        "raw_html": raw_html,
    }

    stats = dict(state.get("stats", {"total": 0, "success": 0, "failed": 0, "skipped": 0}))
    stats["total"] = len(visited)
    stats["success"] += 1

    return {
        **state,
        "url_queue": queue,
        "visited": visited,
        "results": results,
        "stats": stats,
        "current_page": current_page,
        "error": "",
        "nav_mapping": nav_mapping,
    }


# ==================== 节点2: 内容清洗（去头去尾去侧边栏 + 图片绝对路径） ====================

def content_clean_v2_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Phase 1 升级：清洗 + Pydantic 数据校验。

    1. 去头去尾去侧边栏/广告/弹窗
    2. 语义检索正文区域
    3. 图片 src 相对路径 → 绝对路径
    4. ★ Pydantic NewsArticleSchema 校验
    5. 生成标准 HTML 存入 results
    """
    current_page = state.get("current_page", {})
    if not current_page or not current_page.get("raw_html"):
        return {**state, "error": ""}

    url = current_page["url"]
    depth = current_page.get("depth", 1)
    breadcrumb = current_page.get("breadcrumb", [])
    title = current_page.get("title", "")

    clean_start = time.time()
    agent_logger.info(f"content_clean | 开始清洗 | url={url[:80]} | title={title[:30]}")

    raw_html = current_page["raw_html"]
    error_log = list(state.get("error_log", []))
    extracted_data = list(state.get("extracted_data", []))
    nav_mapping = dict(state.get("nav_mapping", {}))
    base_url = state.get("base_url", "")

    try:
        soup = BeautifulSoup(raw_html, "html.parser")

        # 1. 去噪音：脚本/样式/iframe
        for tag in soup(["script", "style", "noscript", "iframe", "link"]):
            tag.decompose()

        # 2. 去头去尾
        _remove_header_footer(soup)

        # 3. 去侧边栏/广告/弹窗
        _remove_sidebar_ads_popups(soup)

        # 4. 寻找正文区域
        content = _find_content_area(soup)
        if not content:
            content = soup.find("body") or soup

        # ★ 剥离原站定位样式（防止内容重叠）
        _strip_positioning_styles(content)

        # ★ 二次清理：移除残留的导航链接列表
        _remove_nav_links(content)

        # 5. 清理内容区空容器
        for div in list(content.find_all("div")):
            txt = div.get_text(strip=True)
            imgs = div.find_all("img")
            videos = div.find_all("video")
            if len(txt) == 0 and not imgs and not videos:
                div.decompose()

        # 6. ★★★ 图片路径绝对化（关键需求）★★★
        img_count = _fix_image_srcs_absolute(content, url)

        # 7. 视频路径绝对化
        for video in content.find_all("video"):
            sv = video.get("src", "")
            if sv and not sv.startswith(("http://", "https://", "data:")):
                video["src"] = urljoin(url, sv)
            for source in video.find_all("source"):
                ss = source.get("src", "")
                if ss and not ss.startswith(("http://", "https://", "data:")):
                    source["src"] = urljoin(url, ss)

        # 8. 链接转绝对URL
        for a in content.find_all("a", href=True):
            href = a["href"]
            if href and not href.startswith(("http://", "https://", "javascript:", "mailto:", "#")):
                a["href"] = urljoin(url, href)

        # 9. 提取发布时间
        riqi = _extract_publish_time(soup)

        # 10. 构建清洗后 HTML
        body_content = str(content)
        has_h1 = content.find("h1") is not None
        title_block = f"<h1>{title}</h1>" if not has_h1 and title else ""
        time_block = f'<p class="publish-time">发布时间：{riqi}</p>' if riqi else ""

        clean_html = f"""<!-- url：{url} -->
<!-- 发布时间：{riqi} -->
<div class="content-wrapper">
{title_block}
{time_block}
{body_content}
</div>"""

        full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="referrer" content="no-referrer">
<title>{title or url}</title>
<style>{_UNIFIED_CSS}</style>
</head>
<body>
{clean_html}
</body>
</html>"""

        # ★ 内容质量过滤：可见文本过少时跳过保存
        if not _check_content_quality(full_html, url):
            stats = dict(state.get("stats", {"total": 0, "success": 0, "failed": 0, "skipped": 0}))
            stats["skipped"] += 1
            agent_logger.warning(
                f"content_clean | 质量过滤跳过 | url={url[:80]} | "
                f"title={title[:40]}"
            )
            return {
                **state,
                "results": list(state.get("results", [])),
                "current_page": {},
                "stats": stats,
                "nav_mapping": nav_mapping,
            }

        results = list(state.get("results", []))
        results.append({
            "url": url,
            "title": title,
            "breadcrumb": breadcrumb,
            "depth": depth,
            "html": full_html,
            "images": img_count,
            "riqi": riqi,
        })

        stats = dict(state.get("stats", {"total": 0, "success": 0, "failed": 0, "skipped": 0}))
        elapsed = time.time() - clean_start
        agent_logger.info(
            f"content_clean | 清洗成功 | url={url[:80]} | "
            f"title={title[:40]} | images={img_count} | 耗时={elapsed:.1f}s"
        )

        # ★ Pydantic 校验：构建 NewsArticleSchema 并验证
        nav_list = get_nav_name(url, soup=None, breadcrumb=breadcrumb,
                                nav_mapping=nav_mapping, base_url=base_url)
        try:
            article = NewsArticleSchema(
                url=url,
                title=title or "未知标题",
                publish_time=riqi if riqi else None,
                breadcrumb=breadcrumb,
                html_content=full_html,
                images_count=img_count,
                nav_levels=nav_list,
            )
            # model_validate 严格校验
            NewsArticleSchema.model_validate(article.model_dump())
            extracted_data.append(article.model_dump())
            agent_logger.info(
                f"content_clean | Pydantic 校验通过 | url={url[:80]} | "
                f"extracted_fields: title={article.title[:30]}, "
                f"nav_levels={article.nav_levels}"
            )
        except Exception as val_err:
            # 校验失败 → 记录为脏数据
            error_log.append({
                "timestamp": datetime.now().isoformat(),
                "node": "content_clean",
                "url": url[:100],
                "error_type": "PydanticValidationError",
                "message": f"数据校验失败: {str(val_err)[:200]}",
            })
            agent_logger.warning(
                f"content_clean | Pydantic 校验失败（脏数据）| url={url[:80]} | "
                f"reason={str(val_err)[:100]}"
            )

        print(f"  [清洗完成] 标题={title} 图片={img_count}张 面包屑={' > '.join(breadcrumb)}")

        return {
            "results": results,
            "current_page": {},
            "stats": stats,
            "error": "",
            "extracted_data": extracted_data,
            "nav_mapping": nav_mapping,
        }

    except Exception as e:
        error_log.append({
            "timestamp": datetime.now().isoformat(),
            "node": "content_clean",
            "url": url[:100],
            "error_type": "EXTRACTION_EXCEPTION",
            "message": f"{type(e).__name__}: {str(e)[:200]}",
        })
        agent_logger.error(
            f"content_clean | 清洗异常 | url={url[:80]} | "
            f"error={type(e).__name__}: {str(e)[:100]}"
        )
        print(f"  [清洗异常] {e}")
        traceback.print_exc()
        # ★ 兜底：清洗失败也保存原始 HTML，不丢失页面
        title = current_page.get("title", "") or url
        breadcrumb = current_page.get("breadcrumb", [])
        depth = current_page.get("depth", 1)
        try:
            fallback_html = f"""<!-- url：{url} -->
<!-- ⚠ 清洗失败，保留原始内容 -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>{_UNIFIED_CSS}</style>
</head>
<body>
<div class="content-wrapper">
<h1>{title}</h1>
{raw_html[:50000]}
</div>
</body>
</html>"""
        except Exception:
            fallback_html = f"<html><body><h1>{url}</h1><p>清洗失败: {e}</p></body></html>"

        results = list(state.get("results", []))
        results.append({
            "url": url, "title": title, "breadcrumb": breadcrumb,
            "depth": depth, "html": fallback_html, "images": 0, "riqi": "",
        })
        return {**state, "results": results, "current_page": {}, "error": ""}


# ==================== 通用工具函数 ====================

def _fetch(url: str, retries: int = 4) -> Optional[str]:
    """反爬增强版下载：Session + Retry + 随机 UA + Referer 伪装 + 随机延迟"""
    session = _get_robust_session()
    
    for attempt in range(retries):
        try:
            # 随机延迟（0.5~2秒），防止触发 IP 封禁
            time.sleep(random.uniform(0.5, 2.0))
            
            # 构建反爬请求头
            parsed_url = urlparse(url)
            headers = {
                "User-Agent": random.choice(_USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Referer": parsed_url._replace(path='/', query='', fragment='').geturl(),
            }
            
            resp = session.get(url, headers=headers, timeout=15, verify=False)
            
            # 429 限流 → 退避重试
            if resp.status_code == 429:
                wait = (attempt + 1) * 8
                print(f"  [fetch] 429 限流，{wait}秒后重试...({attempt+1}/{retries})")
                time.sleep(wait)
                continue
            # 403/404 等客户端错误 → 不重试，直接返回
            if 400 <= resp.status_code < 500:
                print(f"  [fetch] HTTP {resp.status_code} (永久错误)，跳过: {url[:80]}")
                return None
            # 5xx 服务端错误 → 重试
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} Server Error", response=resp)
            resp.raise_for_status()
            # 自动编码
            if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "latin-1"):
                resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except Exception as e:
            if attempt < retries - 1:
                wait = (attempt + 1) * 5
                print(f"  [fetch] 失败: {type(e).__name__}, {wait}秒后重试...({attempt+1}/{retries})")
                time.sleep(wait)
    return None


def _fetch_with_playwright(url: str, timeout: int = 30000) -> Optional[str]:
    """
    使用 Playwright 无头浏览器（Chromium）获取页面完整渲染后的 HTML。
    模拟用户滚动到底部以触发懒加载图片，等待 networkidle 后返回完整 DOM。

    如果 Playwright 未安装或启动失败，返回 None（调用方应 Fallback 到静态抓取）。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ⚠️ [降级] Playwright 未安装，已切换为静态模式，部分动态图片可能丢失。")
        print("     安装方法: pip install playwright && python -m playwright install chromium")
        return None

    print(f"🎭 [动态渲染] 正在使用 Playwright 渲染页面并模拟滚动...")

    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # 1. 导航到页面，等待网络空闲
            page.goto(url, wait_until="networkidle", timeout=timeout)

            # 2. 模拟用户滚动到底部，触发所有懒加载图片
            page.evaluate("""
                async () => {
                    await new Promise((resolve) => {
                        let totalHeight = 0;
                        const distance = 300;
                        const timer = setInterval(() => {
                            window.scrollBy(0, distance);
                            totalHeight += distance;
                            if (totalHeight >= document.body.scrollHeight) {
                                clearInterval(timer);
                                resolve();
                            }
                        }, 200);
                    });
                }
            """)

            # 3. 滚动后再次等待网络空闲（等待懒加载图片请求完成）
            page.wait_for_load_state("networkidle", timeout=timeout)

            # 4. 获取完全渲染后的 HTML
            rendered_html = page.content()

            browser.close()
            print(f"  🎭 [动态渲染] 渲染完成，获取到完整 DOM。")
            return rendered_html

    except Exception as e:
        print(f"  ⚠️ [降级] Playwright 渲染失败: {type(e).__name__}: {e}")
        print(f"  ⚠️ [降级] 已切换为静态模式，部分动态图片可能丢失。")
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        return None


def _normalize_url(url: str) -> str:
    """URL 标准化（去重用）"""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    if parsed.scheme == "http" and ":80" in netloc:
        netloc = netloc.replace(":80", "")
    if parsed.scheme == "https" and ":443" in netloc:
        netloc = netloc.replace(":443", "")
    return urlunparse((parsed.scheme, netloc, path, parsed.params, parsed.query, ""))


def _is_same_domain(base_url: str, target_url: str) -> bool:
    b = urlparse(base_url)
    t = urlparse(target_url)
    return b.netloc.lower() == t.netloc.lower()


def _is_valid_page_url(url: str) -> bool:
    """过滤非 HTML 页面、搜索页、外链跳转页等"""
    lower = url.lower()
    if any(lower.startswith(s) for s in _SKIP_SCHEMES):
        return False
    if lower.endswith(_SKIP_EXTENSIONS):
        return False

    parsed = urlparse(url)

    # 排除搜索/查询页 — 仅当带有搜索参数时才排除（如 /search?q=xxx）
    # 不再一刀切排除 /s, /search, /query 本身（某些网站的栏目可能使用这些路径名）
    search_paths = {"/s", "/search", "/query", "/cse/search"}
    if parsed.path.rstrip("/") in search_paths and parsed.query:
        return False
    # 排除重定向外链页（百度 /link?url=...）— 仅当带有跳转参数时才排除
    redirect_paths = {"/link", "/redirect", "/goto", "/jump", "/url"}
    if parsed.path.rstrip("/") in redirect_paths and parsed.query:
        return False

    # 排除纯锚点（仅当 fragment 长度 > 0 且 path 为根且无 query 时才排除）
    # ★ 放宽规则：只排除 fragment 明显为页面内锚点的情况（如 #section），
    #    不再一刀切排除所有含 fragment 的根路径 URL
    if parsed.fragment and (not parsed.path or parsed.path == "/"):
        if not parsed.query:
            # fragment 长度 > 15 可能是 SPA 路由哈希（如 #/page/about），放行
            if len(parsed.fragment) > 15:
                pass  # 放行，可能是 SPA 路由
            else:
                return False
    return True


# ==================== 导航映射提取 ====================

def _extract_nav_mapping(soup: BeautifulSoup, base_url: str) -> Dict[str, str]:
    """
    从首页 HTML 中提取一级导航栏的（名称 → URL前缀）映射。

    策略：
    1. 查找 <nav> / class/id 含 nav/menu/navbar 的容器
    2. 提取其中所有一级 <a> 标签的文本和 href
    3. 排除"首页"、空链接、外链、过长名称
    4. 返回干净的 {名称: URL前缀} 字典

    Returns: {"关于我们": "/about", "产品中心": "/product", ...}
    """
    nav_mapping = {}
    seen_hrefs = set()

    # 找到导航容器
    nav_container = None
    for el in soup.find_all(["nav", "div", "ul"]):
        cls = " ".join(el.get("class", [])) + " " + (el.get("id") or "")
        if any(kw in cls.lower() for kw in ["nav", "menu", "navbar"]):
            nav_container = el
            break
    if not nav_container:
        nav_container = soup.find("body") or soup

    # 在导航容器中找第一层 <a> 标签：优先找 <ul>/<ol> 下的直接 <li> > <a>
    top_ul = nav_container.find(["ul", "ol"])
    if top_ul:
        candidate_lis = top_ul.find_all("li", recursive=False)
    else:
        # 没有 <ul>，直接在容器中找顶层元素中的 <a>
        candidate_lis = []
        for child in nav_container.find_all(recursive=False):
            if child.name in ("li", "div", "span", "p"):
                candidate_lis.append(child)

    for li in candidate_lis:
        a_tag = None
        if li.name == "li":
            a_tag = li.find("a", href=True, recursive=False) or li.find("a", href=True)
        else:
            a_tag = li.find("a", href=True)

        if not a_tag:
            continue

        name = a_tag.get_text(strip=True)
        href = a_tag.get("href", "").strip()

        # 过滤无效项
        if not name or not href:
            continue
        if name in ("首页", "Home", "home", "网站首页", ""):
            continue
        if len(name) > 20:
            continue
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue

        # 转绝对 URL 后提取路径前缀
        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)

        # 同域名检查
        base_parsed = urlparse(base_url)
        if parsed.netloc.lower() != base_parsed.netloc.lower():
            # 外链，跳过
            continue

        # 提取路径前缀（去掉末尾 /）
        prefix = parsed.path.rstrip("/") or "/"
        if prefix in seen_hrefs:
            continue
        seen_hrefs.add(prefix)

        nav_mapping[name] = prefix

    return nav_mapping


def _get_category_name(url: str, base_url: str, nav_mapping: Dict[str, str]) -> str:
    """
    根据页面 URL 和导航映射，判断该页面属于哪个一级导航分类。

    规则：
    1. 首页：path == "/" 或空
    2. 精确匹配一级导航自身页面
    3. 前缀匹配子页面（必须加 '/' 防止误匹配）
    4. 其他情况 → "其他页面"
    """
    path = urlparse(url).path.rstrip("/")

    # 首页
    if not path or path == urlparse(base_url).path.rstrip("/"):
        return "首页"

    # 1. 精确匹配一级导航自身页面
    for nav_name, nav_path in nav_mapping.items():
        if path == nav_path.rstrip("/"):
            return nav_name

    # 2. 前缀匹配子页面（必须加 '/' 防止误匹配）
    for nav_name, nav_path in nav_mapping.items():
        if path.startswith(nav_path.rstrip("/") + "/"):
            return nav_name

    return "其他页面"


# ==================== 链接发现 ====================

def _extract_same_domain_links(soup: BeautifulSoup, page_url: str, base_url: str) -> List[Tuple[str, str]]:
    """
    提取页面中所有同域内部链接（去重）
    Returns: [(url, text), ...]
    """
    links = []
    seen = set()
    body = soup.find("body") or soup

    for a in body.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        abs_url = urljoin(page_url, href)
        if not _is_same_domain(base_url, abs_url):
            continue
        if not _is_valid_page_url(abs_url):
            continue
        norm = _normalize_url(abs_url)
        if norm not in seen:
            seen.add(norm)
            text = a.get_text(strip=True)[:50]
            links.append((abs_url, text))
    return links


# ==================== 头部/页脚/侧边栏移除 ====================

def _get_element_position_ratio(el, body) -> float:
    """
    估算元素在 body 中的纵向位置比例（0.0=顶部, 1.0=底部）。
    采用前序遍历序数 / 总元素数 近似计算。
    无 body 时返回 -1。
    """
    if not body:
        return -1.0
    all_els = list(body.descendants)
    total = len(all_els)
    if total == 0:
        return -1.0
    for i, descendant in enumerate(all_els):
        if descendant is el:
            return i / total
    return -1.0


def _is_header_element(el, body=None) -> bool:
    """判断是否为头部/导航元素（类名 + 链接密度 + DOM位置）"""
    if not hasattr(el, "name") or el.name is None:
        return False
    if el.name in ("header", "nav"):
        return True

    # ★ 图片保护：含 ≥3 个 img 的元素大概率是内容区，不判定为头部
    if len(el.find_all("img")) >= 3:
        return False

    text = el.get_text(strip=True)
    text_len = len(text)
    if text_len == 0:
        return True

    # 链接密度判断
    links = el.find_all("a")
    if links:
        link_text = sum(len(l.get_text(strip=True)) for l in links)
        non_link_text = text_len - link_text
        if non_link_text >= 200:
            return False
        if text_len > 0 and link_text / text_len > 0.55 and text_len < 600:
            return True

    # class/id 特征
    cls_str = " ".join(el.get("class", [])) if el.get("class") else ""
    id_str = (el.get("id") or "").lower()
    combined = (cls_str + " " + id_str).lower()
    for pattern in _HEADER_PATTERNS:
        if pattern in combined:
            return True

    # ★ DOM 位置：body 前 15% 且链接密度 >40% 视为头部
    if body and links and text_len > 0:
        pos = _get_element_position_ratio(el, body)
        if 0.0 <= pos < 0.15:
            link_ratio = sum(len(l.get_text(strip=True)) for l in links) / text_len
            if link_ratio > 0.4 and text_len < 2000:
                return True

    return False


def _is_footer_element(el, body=None) -> bool:
    """判断是否为页脚元素（类名 + 版权文本 + DOM位置）"""
    if not hasattr(el, "name") or el.name is None:
        return False
    if el.name == "footer":
        return True

    text = el.get_text(strip=True)
    text_len = len(text)
    if text_len == 0:
        return True

    cls_str = " ".join(el.get("class", [])) if el.get("class") else ""
    for pattern in ["footer", "footer-bottom", "footer-top",
                    "copyright", "copyright-info",
                    "site-footer", "page-footer"]:
        if pattern in cls_str.lower():
            return True

    # 页脚文本特征
    footer_match_len = 0
    for pat in _FOOTER_TEXT_PATTERNS:
        for m in re.finditer(pat, text):
            footer_match_len += len(m.group())
    if footer_match_len > 0 and text_len > 200 and footer_match_len < text_len * 0.5:
        return False
    if footer_match_len > 0:
        return True

    # ★ DOM 位置：body 后 20% 且文本<2000 → 视为页脚
    if body:
        pos = _get_element_position_ratio(el, body)
        if 0.80 <= pos <= 1.0 and text_len < 2000:
            # 需要同时有小文本特征或链接稀疏
            links = el.find_all("a")
            link_text = sum(len(l.get_text(strip=True)) for l in links) if links else 0
            if link_text < text_len * 0.5:
                return True

    return False


def _remove_header_footer(soup: BeautifulSoup):
    """
    通用去头去尾（类名 + DOM 位置双重判定）。
    
    ★ 自适应策略：
      - 图片 ≥ 20 张 → 仅删除显式 <header>/<nav>/<footer> 标签，保留所有内容区
      - 图片 < 20 张 → 启用完整启发式清理（类名 + 位置），适合纯文本文章页
    """
    body = soup.find("body")
    total_imgs = len(body.find_all("img")) if body else 0
    
    for tag in soup.find_all(["header", "nav", "footer"]):
        tag.decompose()
    
    if not body:
        return
    
    # ★ 高图片密度页面：仅做最保守的清理，保护所有图片内容
    if total_imgs >= 20:
        # 删除真正的空容器（无文本 且 无img/video/iframe）
        for el in list(soup.find_all(["div", "section"])):
            if not hasattr(el, "name"):
                continue
            if el.get_text(strip=True) == "" and not el.find_all(["img", "video", "iframe"]):
                el.decompose()
                continue
        return

    # 以下为低图片页面（文章/详情页）的完整启发式清理
    # 基于位置的头尾清理：body 最前/最后子元素
    direct_children = [c for c in list(body.children) if hasattr(c, "name")]
    if len(direct_children) >= 3:
        # 头部候选：前 1~2 个子元素
        for child in direct_children[:2]:
            if hasattr(child, "name") and _is_header_element(child, body):
                child.decompose()
        # 页脚候选：后 1~2 个子元素
        for child in direct_children[-2:]:
            if hasattr(child, "name") and _is_footer_element(child, body):
                child.decompose()

    # 遍历所有 div/section 做精细化清理
    for el in list(soup.find_all(["div", "section"])):
        if not hasattr(el, "name"):
            continue
        if el.get_text(strip=True) == "":
            el.decompose()
            continue
        # ★ 图片保护：如果一个 div 包含 ≥3 个 img，保留（可能是轮播/业务展示等主要内容）
        if len(el.find_all("img")) >= 3:
            continue
        if _is_header_element(el, body) or _is_footer_element(el, body):
            el.decompose()


def _remove_sidebar_ads_popups(soup: BeautifulSoup):
    """删除侧边栏、悬浮广告、弹窗 + 详情页元数据噪音"""
    body = soup.find("body")
    total_imgs = len(body.find_all("img")) if body else 0
    
    for tag in soup.find_all(["aside"]):
        tag.decompose()

    for kw in ["sidebar", "side-bar", "side_bar", "widget-area",
               "left-panel", "right-panel"]:
        for el in list(soup.find_all(class_=re.compile(kw, re.I))):
            if len(el.find_all("img")) < 3:
                el.decompose()
        for el in list(soup.find_all(id=re.compile(kw, re.I))):
            if len(el.find_all("img")) < 3:
                el.decompose()

    for kw in ["modal", "popup", "pop-up", "dialog", "overlay",
               "lightbox", "tooltip"]:
        for el in list(soup.find_all(class_=re.compile(kw, re.I))):
            if len(el.find_all("img")) < 3:
                el.decompose()
        for el in list(soup.find_all(id=re.compile(kw, re.I))):
            if len(el.find_all("img")) < 3:
                el.decompose()

    for kw in ["advertisement", "adsense", "banner-ad", "sponsor",
               "-ad-", "google-ad", "dfp-ad"]:
        for el in list(soup.find_all(class_=re.compile(kw, re.I))):
            el.decompose()
        for el in list(soup.find_all(id=re.compile(kw, re.I))):
            el.decompose()

    # fixed/sticky 悬浮
    for el in list(soup.find_all(style=re.compile(r"position\s*:\s*(fixed|sticky)", re.I))):
        if len(el.find_all("img")) < 3:
            el.decompose()

    # ★ 高图片密度页面（首页/多区块页面）：跳过元数据噪音清理，避免误删内容
    if total_imgs >= 20:
        return

    # 以下为低图片页面的元数据/详情页噪音清理
    _remove_article_metadata_noise(soup)
    _remove_back_to_list_links(soup)


def _remove_article_metadata_noise(soup: BeautifulSoup):
    """
    移除详情页文章末尾的元数据噪音：
    文案：xxx、编辑：xxx、审核：xxx、签发：xxx、
    公司地址、咨询电话、二维码关注引导等
    """
    # 排除类名特征
    _META_CLASS_PATTERNS = [
        "author-info", "author", "editor", "reviewer",
        "article-meta", "post-meta", "news-meta",
        "info-source", "source", "meta-info",
        "article-footer", "news-footer", "detail-footer",
        "statement", "declare", "disclaimer",
        "qrcode", "qr-code", "wechat", "wx",
        "contact-info", "company-info", "addr",
        "address", "footer-", "copyright",
        "hotline", "service-tel",
    ]

    for kw in _META_CLASS_PATTERNS:
        for el in list(soup.find_all(class_=re.compile(kw, re.I))):
            el.decompose()
        for el in list(soup.find_all(id=re.compile(kw, re.I))):
            el.decompose()

    # 文本内容关键字匹配：文案：/ 编辑：/ 审核：/ 签发：/ 咨询电话 / 公司地址 等
    _META_TEXT_KEYWORDS = [
        "文案：", "文案:", "编辑：", "编辑:", "责任编辑：", "责任编辑:",
        "审核：", "审核:", "签发：", "签发:", "核对：", "核对:",
        "作者：", "作者:", "来源：", "来源:", "供稿：", "供稿:",
        "咨询电话", "咨询热线", "服务热线",
        "公司地址", "集团地址", "通讯地址", "联系地址",
        "电子邮箱", "邮箱：", "邮箱:",
        "邮政编码", "传真：", "传真:",
        "扫一扫", "扫码关注", "关注我们", "微信号",
        "【版权", "【免责", "声明】",
    ]

    # 查找所有 div/p/span 叶子节点，匹配关键字即删除
    for el in list(soup.find_all(["div", "p", "span", "li", "section"])):
        text = el.get_text(strip=True)
        if not text:
            continue

        # 查找子元素中的 img，如果有超过1个图片，可能是内容区域，谨慎处理
        imgs = el.find_all("img")
        if imgs and len(imgs) <= 1:
            # 文本很短且包含元数据关键字 → 删除
            for kw in _META_TEXT_KEYWORDS:
                if kw in text and len(text) < 200:
                    el.decompose()
                    break
        elif not imgs:
            for kw in _META_TEXT_KEYWORDS:
                if kw in text and len(text) < 200:
                    el.decompose()
                    break


def _remove_back_to_list_links(soup: BeautifulSoup):
    """删除文章底部的「返回列表」「上一篇」「下一篇」等导航链接"""
    _BACK_PATTERNS = [
        "返回列表", "返回上一页", "返回首页",
        "上一篇", "下一篇", "上一条", "下一条",
        "没有了", "已是最后", "已是第一",
    ]
    for el in list(soup.find_all(["a", "span", "p", "div"])):
        text = el.get_text(strip=True)
        if 2 <= len(text) <= 30:
            for pat in _BACK_PATTERNS:
                if pat in text:
                    parent = el.parent
                    el.decompose()
                    # 如果父级只剩空壳，一并删除
                    if parent and parent.name in ("div", "p") and not parent.get_text(strip=True):
                        parent.decompose()
                    break


# ==================== 图片路径绝对化 ★★★ ====================

def _fix_image_srcs_absolute(content, page_url: str) -> int:
    """
    扫描所有 <img> 标签，将相对路径补全为绝对路径。
    同时处理懒加载属性（data-src, data-original 等）和 CSS 背景图。
    不下载图片，只修改 src 属性以支持远程加载。

    Returns: 处理的图片数量
    """
    count = 0

    # ===== 1. 处理 <img> 标签 + 懒加载 + srcset + 协议相对URL =====
    for img in content.find_all("img"):
        # 收集所有可能的 src 属性（懒加载属性优先存放真实URL）
        src_candidates = []
        for attr in ["src", "data-src", "data-original", "data-original-src",
                     "data-lazy-src", "data-url", "data-bg",
                     "data-background", "data-image", "data-img"]:
            val = img.get(attr, "").strip()
            if val and not val.startswith("data:"):
                src_candidates.append((attr, val))

        # 如果 img 在 <picture> 内，优先取同级 <source> 的 srcset
        parent = img.parent if img.parent else None
        picture_src = None
        if parent and parent.name == "picture":
            for source in parent.find_all("source"):
                srcset_val = (source.get("srcset") or source.get("data-srcset") or "").strip()
                if srcset_val:
                    first_url = srcset_val.split(",")[0].strip().split(" ")[0]
                    if first_url and not first_url.startswith("data:"):
                        picture_src = first_url
                        break

        if not src_candidates and not picture_src:
            continue

        # 确定最佳图片源：<picture> > data-src 懒加载 > src
        if picture_src:
            best_src = picture_src
            best_attr = "src"
        else:
            best_attr, best_src = src_candidates[0]
            for attr, val in src_candidates:
                if attr.startswith("data-"):
                    best_attr, best_src = attr, val
                    break

        # URL 补全（含协议相对 // 处理）
        if best_src:
            if best_src.startswith("//"):
                abs_src = "https:" + best_src
            elif not best_src.startswith(("http://", "https://")):
                abs_src = urljoin(page_url, best_src)
            else:
                abs_src = best_src
            img["src"] = abs_src

        # 清理可能导致问题的属性
        for attr in ["data-srcset", "srcset"]:
            if img.get(attr):
                del img[attr]
        for attr in ["width", "height"]:
            if img.get(attr):
                del img[attr]
        img["referrerpolicy"] = "no-referrer"

        count += 1

    # ===== 1b. 处理 <picture> 标签中的 <source> (srcset → 绝对路径) =====
    for picture in content.find_all("picture"):
        for source in picture.find_all("source"):
            for attr_name in ["srcset", "data-srcset"]:
                srcset_val = source.get(attr_name, "").strip()
                if not srcset_val:
                    continue
                # 解析 srcset 中的每个 URL（格式如 "img.jpg 1x, img2x.jpg 2x"）
                parts = re.split(r',\s*', srcset_val)
                new_parts = []
                for part in parts:
                    tokens = part.strip().split()
                    if not tokens:
                        continue
                    url_token = tokens[0]
                    if url_token.startswith("//"):
                        url_token = "https:" + url_token
                    elif not url_token.startswith(("http://", "https://", "data:")):
                        url_token = urljoin(page_url, url_token)
                    tokens[0] = url_token
                    new_parts.append(" ".join(tokens))
                source[attr_name] = ", ".join(new_parts)

    # ===== 2. 处理 CSS background-image（轮播图/Banner/背景大图） =====
    count += _fix_css_background_images(content, page_url)

    return count


def _fix_css_background_images(content, page_url: str) -> int:
    """
    扫描所有元素的内联 style 属性，提取 background-image: url(...)，
    将相对路径补全为绝对路径。
    对于 .swiper-slide / .banner / .slide 等容器上的大图背景，
    将其转换为可见的 <img> 标签插入到容器中。

    Returns: 新增/修复的图片数量
    """
    count = 0
    bg_re = re.compile(r'background(?:-image)?\s*:\s*url\(["\']?([^"\'()]+)["\']?\)', re.I)

    # 轮播/横幅容器特征类名
    banner_patterns = ["banner", "slide", "swiper", "carousel", "hero",
                       "jumbotron", "slider", "ban", "bg-img", "bg-image",
                       "bgimg", "bgimage", "pic", "figure"]

    for el in list(content.find_all(style=True)):
        style = el.get("style", "")
        if not style or "background" not in style.lower():
            continue

        matches = bg_re.findall(style)
        if not matches:
            continue

        cls_str = " ".join(el.get("class", [])).lower() if el.get("class") else ""
        el_id = (el.get("id") or "").lower()
        combined = cls_str + " " + el_id

        is_banner = any(p in combined for p in banner_patterns)

        for bg_url in matches:
            bg_url = bg_url.strip()
            if not bg_url or bg_url.startswith("data:"):
                continue

            # 补全为绝对路径
            abs_url = bg_url if bg_url.startswith(("http://", "https://")) else urljoin(page_url, bg_url)

            # 替换 style 中的相对 URL 为绝对 URL
            # 同时处理带引号和不带引号的情况
            for quote in ['"', "'", ""]:
                old_val = f'url({quote}{bg_url}{quote})'
                new_val = f'url({quote}{abs_url}{quote})'
                if old_val in style:
                    style = style.replace(old_val, new_val, 1)
                    break

            el["style"] = style

            # 如果是 Banner/轮播容器 → 额外插入 <img> 标签确保可见
            if is_banner:
                existing_img = el.find("img")
                if not existing_img:
                    new_img = content.new_tag("img", src=abs_url,
                                              style="width:100%;height:auto;display:block;")
                    new_img["referrerpolicy"] = "no-referrer"
                    el.insert(0, new_img)
                    count += 1

            count += 1

    # ===== 3. 处理 data-background 等自定义属性（部分主题框架） =====
    for el in list(content.find_all(["div", "section", "li", "figure"])):
        for attr in ["data-background", "data-bg", "data-image", "data-img-src",
                     "data-src", "data-original", "data-original-src"]:
            val = el.get(attr, "").strip()
            if not val or val.startswith("data:") or val.startswith("#"):
                continue

            # 检查是否为图片 URL
            is_img_url = any(val.lower().endswith(ext) for ext in
                           [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"])
            has_img_ext = re.search(r'\.(jpe?g|png|gif|webp|bmp|svg)($|\?|#)', val, re.I)

            if not (is_img_url or has_img_ext):
                # 也可能是无扩展名的图片 URL，通过包含 /upload/ /image/ /img/ 判断
                if not any(kw in val.lower() for kw in ["/upload", "/image", "/img", "/pic", "/photo"]):
                    continue

            abs_url = val if val.startswith(("http://", "https://")) else urljoin(page_url, val)

            cls_str = " ".join(el.get("class", [])).lower() if el.get("class") else ""
            is_banner = any(p in cls_str for p in banner_patterns)

            if is_banner:
                existing_img = el.find("img")
                if not existing_img:
                    new_img = content.new_tag("img", src=abs_url,
                                              style="width:100%;height:auto;display:block;")
                    new_img["referrerpolicy"] = "no-referrer"
                    el.insert(0, new_img)
                    count += 1

            # 也修补该属性为绝对路径
            el[attr] = abs_url

    return count


# ==================== 正文区域提取 ====================

def _find_content_area(soup: BeautifulSoup):
    """
    通用正文识别（多策略融合，不依赖特定类名）：
    策略1: <article> 语义标签
    策略2: 类名关键字 + 结构特征加权
    策略3: 文本密度 + 段落/图片比例 综合评分
    策略4: body 降级
    策略5: ★ 图片保留检查 — 如果最佳候选丢失大量图片，用 body 兜底
    """
    body = soup.find("body")
    if not body:
        return soup

    # 先统计整页图片总数（用于兜底判断）
    total_imgs_in_body = len(body.find_all("img")) if body else 0

    # 策略1: <article> 标签（语义级，最可靠）
    article = soup.find("article")
    if article and len(article.get_text(strip=True)) > 30:
        article_imgs = len(article.find_all("img"))
        # 如果 article 丢失了超过 50% 的图片且总数 > 5，回退到 body
        if total_imgs_in_body > 5 and article_imgs < total_imgs_in_body * 0.5:
            print(f"  ⚠️ [正文识别] article 仅有 {article_imgs}/{total_imgs_in_body} 张图片，回退到 body")
            return body
        return article

    # 策略2: 类名关键字 + DOM位置/结构特征 联合评分
    content_kw = ["content", "main", "article", "detail", "body", "pagebody",
                  "text", "news", "info", "post"]
    candidates = []
    for el in body.find_all(["div", "section"]):
        if _is_header_element(el, body) or _is_footer_element(el, body):
            continue
        txt = el.get_text(strip=True)
        if len(txt) < 40:
            continue
        cls_id = " ".join(el.get("class", [])).lower() + " " + (el.get("id") or "").lower()
        kw_hit = any(kw in cls_id for kw in content_kw)
        p_cnt = len(el.find_all("p"))
        img_cnt = len(el.find_all("img"))
        pos = _get_element_position_ratio(el, body)

        # 评分：文本量 + 段落奖励 + 图片奖励 + 关键字命中加成 + 居中位置加分
        score = len(txt) * 0.5 + p_cnt * 100 + img_cnt * 250
        if kw_hit:
            score *= 1.5  # 类名暗示正文，大幅加权
        if 0.15 <= pos <= 0.85:
            score *= 1.2  # 位于 body 中间位置，更可能是正文
        if score > 200:
            candidates.append((score, el))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        # ★ 多区块合并模式：取 Top 3 候选人，合并为一个容器（宁多勿少）
        top_candidates = candidates[:3]
        # 去重：跳过已被父级包含的子级
        merged_blocks = []
        for i, (score, el) in enumerate(top_candidates):
            is_child = False
            for j, (_, other) in enumerate(top_candidates):
                if i != j and el in other.descendants:
                    is_child = True
                    break
            if not is_child:
                merged_blocks.append((score, el))

        if len(merged_blocks) >= 2:
            # 多个独立内容块 → 合并
            merged = soup.new_tag("div")
            merged["class"] = "merged-content-blocks"
            for _, el in merged_blocks:
                clone = soup.new_tag(el.name)
                for attr, val in el.attrs.items():
                    clone[attr] = val
                for child in list(el.children):
                    clone.append(child)
                merged.append(clone)
            merged_imgs = len(merged.find_all("img"))
            if total_imgs_in_body > 3 and merged_imgs < total_imgs_in_body * 0.4:
                print(f"  ⚠️ [正文识别] 合并后仅含 {merged_imgs}/{total_imgs_in_body} 张图片，回退到 body")
                return body
            print(f"  🔀 [正文识别] 多区块合并：Top {len(merged_blocks)} 块 → 保留 {merged_imgs}/{total_imgs_in_body} 张图片")
            return merged
        else:
            # 只有1个独立块 → 走原来的单块逻辑
            best = merged_blocks[0][1]
            best_imgs = len(best.find_all("img"))
            if total_imgs_in_body > 3 and best_imgs < total_imgs_in_body * 0.4:
                print(f"  ⚠️ [正文识别] 最佳候选仅含 {best_imgs}/{total_imgs_in_body} 张图片，回退到 body（首页/多区块页面）")
                return body
            return best

    # 策略3: 全局文本密度评分（无类名时兜底）
    best_el, best_score = None, 0
    body_txt_len = len(body.get_text(strip=True))
    for el in body.find_all(["div", "section", "article"]):
        if _is_header_element(el, body) or _is_footer_element(el, body):
            continue
        txt = el.get_text(strip=True)
        if len(txt) < 20:
            continue
        if body_txt_len > 0 and len(txt) > body_txt_len * 0.8:
            is_body_self = el is body or (el.parent and el.parent is body)
            if is_body_self:
                continue
        p_cnt = len(el.find_all("p"))
        img_cnt = len(el.find_all("img"))
        # 文本密度 = 净文本 / HTML长度（避免全是链接的导航）
        html_len = len(str(el))
        density = len(txt) / max(html_len, 1)
        score = len(txt) * density * 10 + p_cnt * 80 + img_cnt * 200
        if score > best_score and len(txt) > 30:
            best_score = score
            best_el = el

    if best_el:
        best_imgs = len(best_el.find_all("img"))
        if total_imgs_in_body > 3 and best_imgs < total_imgs_in_body * 0.4:
            print(f"  ⚠️ [正文识别] 密度候选仅含 {best_imgs}/{total_imgs_in_body} 张图片，回退到 body")
            return body
        return best_el

    # ★ 策略4补充：多区块合并模式 — 保留所有含图片的内容块
    if total_imgs_in_body > 10:
        # 高图片页面：将所有非导航/非页脚的含图片 div 合并为一个容器
        print(f"  🔀 [正文识别] 高图片页面（{total_imgs_in_body} 张），启用多区块合并模式")
        merged = soup.new_tag("div")
        merged["class"] = "merged-content"
        for el in body.find_all(["div", "section"]):
            if _is_header_element(el, body) or _is_footer_element(el, body):
                continue
            imgs = el.find_all("img")
            if len(imgs) >= 2:
                # 含 2 张以上图片的非导航块 → 保留
                merged.append(el)
        # 如果合并容器中包含了至少 50% 的原始图片，使用合并结果
        merged_imgs = len(merged.find_all("img"))
        if merged_imgs >= total_imgs_in_body * 0.5:
            print(f"  ✅ [正文识别] 多区块合并：保留了 {merged_imgs}/{total_imgs_in_body} 张图片")
            return merged
        else:
            print(f"  ⚠️ [正文识别] 多区块合并仅含 {merged_imgs}/{total_imgs_in_body} 张图片，回退到 body")

    # 策略5: body 兜底
    return body


# ==================== 样式清洗 ====================

def _strip_positioning_styles(content):
    """
    剥离原站内联样式中的定位属性，防止内容重叠。
    保留颜色、字体等视觉样式，仅移除 position/float/left/top 等布局属性。
    """
    _POSITIONING_RE = re.compile(
        r'(^|;)\s*(position|float|left|right|top|bottom|z-index|display|'
        r'visibility|opacity|transform|transition|animation)[^;]*;?',
        re.IGNORECASE
    )
    for el in list(content.find_all(style=True)):
        style = el.get("style", "")
        if not style:
            continue
        # 移除定位相关属性
        cleaned = _POSITIONING_RE.sub('', style)
        # 清理多余分号
        cleaned = re.sub(r';+', ';', cleaned).strip(';').strip()
        if cleaned:
            el["style"] = cleaned
        else:
            del el["style"]


def _remove_nav_links(content):
    """
    二次清理：移除残留的导航链接列表。
    针对包含大量短链接（<a>标签）且无段落文本的容器。
    """
    # 关键词黑名单：含这些类名/ID的容器整体删除
    _NAV_KEYWORDS = [
        "nav", "menu", "footer", "sidebar", "widget", "breadcrumb",
        "pagination", "pager", "tab", "tabs", "toolbar", "tool-bar",
        "header", "topbar", "top-bar", "head", "banner",
        "copyright", "bottom", "foot", "link-list", "linklist",
        "navigation", "sitemap", "site-map",
        "contact-info", "address", "hotline", "qrcode", "qr-code",
        "share", "social", "follow", "subscribe",
        "login", "register", "sign", "search-box", "search",
        "language", "lang", "dropdown", "drop-down",
    ]
    for kw in _NAV_KEYWORDS:
        for el in list(content.find_all(class_=re.compile(kw, re.I))):
            # 保护含图片的容器
            if len(el.find_all("img")) > 0:
                continue
            el.decompose()
        for el in list(content.find_all(id=re.compile(kw, re.I))):
            if len(el.find_all("img")) > 0:
                continue
            el.decompose()

    # 链接密度清理：如果 ul/div 内全是 <a> 标签（>80% 文本来自链接），删除
    for el in list(content.find_all(["ul", "ol", "div"])):
        links = el.find_all("a")
        if not links or len(links) < 3:
            continue
        # 保护：如果含 img 或段落文本 > 200 字符，跳过
        if el.find_all("img"):
            continue
        total_text = len(el.get_text(strip=True))
        if total_text == 0:
            el.decompose()
            continue
        link_text = sum(len(a.get_text(strip=True)) for a in links)
        # 链接文本占比 > 80% 且总链接数 ≥ 5 → 判定为导航列表
        if link_text / total_text > 0.8 and len(links) >= 5:
            el.decompose()


# ==================== 面包屑提取 ====================

def _parse_breadcrumb(soup: BeautifulSoup) -> List[str]:
    """
    提取面包屑导航文字，忽略"首页"/"Home"
    Returns: ["关于我们", "企业文化"]
    """
    items = []

    for kw in ["breadcrumb", "bread", "path", "location"]:
        bc = soup.find(class_=re.compile(kw, re.I))
        if not bc:
            bc = soup.find(id=re.compile(kw, re.I))
        if not bc:
            bc = soup.find(attrs={"aria-label": re.compile(kw, re.I)})
        if bc:
            for a in bc.find_all("a"):
                text = a.get_text(strip=True)
                if text and text not in ("首页", "主页", "Home", "home", "网站首页"):
                    items.append(text)
            # 检查当前页面文本（非链接）
            spans = bc.find_all("span")
            if spans:
                last = spans[-1].get_text(strip=True)
                if last and last not in ("首页", "主页", "Home", "home", ">"):
                    if last not in items:
                        items.append(last)
            if items:
                return items

    return items


# ==================== 多级文件夹路径构建 ====================

def _build_folder_path(breadcrumb: List[str], page_url: str, root_url: str, output_root: str) -> str:
    """
    根据面包屑或 URL 层级构建多级文件夹路径
    优先级：面包屑 > URL 路径层级

    示例：
      面包屑 ["关于我们","企业文化"] → output/domain/关于我们/企业文化/
      URL  /news/company/123.html → output/domain/news/company/123/
    """
    if breadcrumb:
        # 使用面包屑
        parts = [_safe_filename(b) for b in breadcrumb if b]
        if parts:
            return os.path.join(output_root, *parts)

    # 降级：URL 路径层级
    parsed = urlparse(page_url)
    path = parsed.path.strip("/")
    if path:
        segments = path.split("/")
        # 过滤空段，限制深度为 4 层
        valid_segments = [_safe_filename(s) for s in segments if s and s not in ("index.html", "index.htm", "index.asp", "index.php", "default.html", "default.asp")]
        if valid_segments:
            return os.path.join(output_root, *valid_segments[:4])

    # 根路径兜底
    return os.path.join(output_root, "_root")


def _page_filename(title: str, page_url: str, breadcrumb: List[str]) -> str:
    """生成页面文件名"""
    # 优先使用标题
    if title and title != "无标题":
        return _safe_filename(title) + ".html"

    # 面包屑最后一段
    if breadcrumb:
        return _safe_filename(breadcrumb[-1]) + ".html"

    # URL 最后一段
    parsed = urlparse(page_url)
    path = parsed.path.strip("/")
    if path:
        last = path.split("/")[-1]
        # 移除扩展名后作为文件名
        base = os.path.splitext(last)[0]
        if base and base not in ("index", "default"):
            return _safe_filename(base) + ".html"

    return "page.html"


def _safe_filename(name: str) -> str:
    """安全文件名"""
    if not name:
        return "untitled"
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = name.strip().strip(".")
    if len(name) > 60:
        name = name[:60]
    return name


# ==================== 智能分类 + 路径生成 ====================

def determine_category(soup, url):
    """通用分类：自动提取面包屑或Title中的第一级分类"""
    path = urlparse(url).path.lower()
    
    # 1. 首页强制识别（兼容各种首页URL格式）
    if path in ['', '/', '/index.html', '/index.shtml', '/index.htm', '/index.php'] or ('?' not in url and path.endswith('/')):
        return "首页"
        
    # 2. 优先提取面包屑导航的第二级 (如: 首页 > 汇能资讯 > 集团新闻 -> 提取"汇能资讯")
    breadcrumbs = soup.select('.breadcrumb a, .crumb a, .bread a, .weizhi a, [class*="bread"] a, [class*="crumb"] a, [class*="position"] a')
    if len(breadcrumbs) >= 2:
        cat = breadcrumbs[1].get_text(strip=True)
        if cat and cat != "首页" and 1 < len(cat) <= 10:
            return clean_filename(cat)
            
    # 3. 其次从 <title> 提取 (如 "集团新闻-汇能控股集团" -> "集团新闻")
    title = soup.find('title')
    if title:
        # 使用 - | _ — 分割，通常第一段或第二段就是栏目名
        parts = re.split(r'[-|_—–]', title.get_text(strip=True))
        for p in parts:
            p = p.strip()
            # 排除公司名和通用词，提取真正的分类词
            if p and not any(kw in p for kw in ['公司', '集团', '官网', '首页', '国家能源', '龙源', '汇能', 'ceic', 'chnenergy', 'huineng']):
                if 1 < len(p) <= 10:
                    return clean_filename(p)
                    
    # 4. 兜底：放入"其他"
    return "其他"


def is_content_valid(soup, url):
    """放宽拦截条件，避免误杀正常页面"""
    # ★ 只要被判定为"首页"，无条件放行，禁止拦截
    if determine_category(soup, url) == "首页":
        return True
        
    # 列表页无条件放行
    if 'list' in url or 'index' in url:
        return True
        
    # 1. 检查是否有图片、视频或列表链接 (有这些就算正文少，也是有价值的页面)
    if soup.find('img') or soup.find('video') or len(soup.find_all('a')) > 10:
        return True
        
    # 2. 纯文本检查 (将阈值从 100 降低到 30，避免误杀短新闻)
    text = soup.get_text(strip=True)
    if len(text) < 30:
        print(f"⚠️ [拦截] 内容极少({len(text)}字)且无多媒体，丢弃: {url}")
        return False
        
    return True


def generate_smart_path(soup, url, category):
    """生成智能路径：分类/年份/文件名.html"""
    file_name = ""
    title = soup.find('title')
    if title:
        for p in re.split(r'[-|_—–]', title.get_text(strip=True)):
            p = p.strip()
            if p and not any(kw in p for kw in ['公司', '集团', '官网', '首页', '龙源', '汇能']):
                file_name = clean_filename(p)
                break
    if not file_name and soup.find('h1'):
        file_name = clean_filename(soup.find('h1').get_text(strip=True))
    if not file_name:
        url_path = urlparse(url).path.strip('/').split('/')[-1]
        file_name = clean_filename(re.sub(r'\.(html|shtml|htm|php)$', '', url_path, flags=re.IGNORECASE))
    if not file_name:
        file_name = "未命名"
    safe_name = file_name + ".html"

    year_match = re.search(r'(20\d{2})', url) or re.search(r'(20\d{2})', soup.get_text())
    if year_match and category not in ["首页", "其他"]:
        return os.path.join(category, year_match.group(1), safe_name)
    return os.path.join(category, safe_name)


# ==================== 标题和时间提取 ====================

def _extract_page_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        text = h1.get_text(strip=True)
        if text and len(text) >= 2:
            return text
    title_cls = soup.find(class_=re.compile(r"article-title|post-title|news-title|detail-title", re.I))
    if title_cls:
        text = title_cls.get_text(strip=True)
        if text:
            return text
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


def _extract_publish_time(soup: BeautifulSoup) -> str:
    time_tag = soup.find("time")
    if time_tag:
        dt = time_tag.get("datetime", "") or time_tag.get_text(strip=True)
        return _normalize_datetime(dt)
    time_cls = soup.find(class_=re.compile(r"time|date|publish|pubtime|post-time|article-time", re.I))
    if time_cls:
        return _normalize_datetime(time_cls.get_text(strip=True))
    text = soup.get_text()
    for pat in [r'(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?\s*\d{1,2}:\d{2}(:\d{2})?)',
                r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})']:
        m = re.search(pat, text)
        if m:
            return _normalize_datetime(m.group(1))
    return ""


def _normalize_datetime(dt_str: str) -> str:
    if not dt_str:
        return ""
    dt_str = dt_str.strip().replace("年", "-").replace("月", "-").replace("日", "")
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
        try:
            from datetime import datetime
            return datetime.strptime(dt_str, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return dt_str


# ==================== 图片抢救节点 ★★★ ====================

# 懒加载属性候选列表（按优先级排列）
_LAZY_ATTRS = ["data-src", "data-original", "data-lazy-src", "data-actualsrc",
               "data-url", "data-img", "data-pic", "data-bg", "data-background"]

# 占位图特征关键词
_PLACEHOLDER_KW = ["data:image", "1x1", "placeholder", "lazy", "spacer",
                   "blank.gif", "blank.png", "loading.gif", "loading.svg",
                   "transparent", "pixel.gif", "pixel.png", "empty.gif",
                   "grey.gif", "ajax-loader", "grey-pixel", "white-pixel"]


def _is_placeholder_src(src: str) -> bool:
    """判断 src 是否为占位图"""
    if not src:
        return True
    lower = src.lower()
    return any(kw in lower for kw in _PLACEHOLDER_KW)


def image_rescue_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    图片抢救节点 — 5 层策略逐一抢救图片，确保最终 HTML 中的图片可正常显示。

    在 fetch_node（获取HTML）之后、content_clean（内容清洗）之前执行。
    直接修改 current_page["raw_html"]，其他字段不变。

    5 层策略：
      第1层 — 路径补全：相对路径 → 绝对路径
      第2层 — 懒加载破解：从 data-src 等属性提取真实 URL
      第3层 — CSS 背景图提取：background-image → <img>
      第4层 — 防盗链绕过：注入 <meta name="referrer" content="no-referrer">
      第5层 — 可选图片本地化：下载到本地 images/ 子目录

    Returns: 更新后的 state（current_page["raw_html"] 被替换）
    """
    import config as cfg

    current_page = state.get("current_page", {})
    if not current_page or not current_page.get("raw_html"):
        return {**state}

    url = current_page.get("url", "")
    base_url = state.get("base_url", "")
    raw_html = current_page["raw_html"]

    # 统计计数器
    stats_count = {"total": 0, "path_fixed": 0, "lazy_fixed": 0,
                   "css_extracted": 0, "downloaded": 0, "download_failed": 0}

    try:
        soup = BeautifulSoup(raw_html, "html.parser")
        # 根据当前页的 URL 确定真正的 base（而非 state 中的 base_url）
        page_base = url if url else base_url

        # ===== 第1层：路径补全 =====
        for img in soup.find_all("img"):
            src = img.get("src", "").strip()
            if not src or src.startswith("data:"):
                stats_count["total"] += 1
                continue
            stats_count["total"] += 1
            # 协议相对 URL → 补全
            if src.startswith("//"):
                img["src"] = "https:" + src
                stats_count["path_fixed"] += 1
            # 相对路径 → 绝对路径
            elif not src.startswith(("http://", "https://")):
                try:
                    img["src"] = urljoin(page_base, src)
                    stats_count["path_fixed"] += 1
                except Exception:
                    pass

        # ===== 第2层：懒加载破解 =====
        for img in soup.find_all("img"):
            src = img.get("src", "").strip()
            # 判断 src 是否为占位图
            if _is_placeholder_src(src):
                # 遍历懒加载属性找到第一个 http 开头的真实 URL
                for attr in _LAZY_ATTRS:
                    val = img.get(attr, "").strip()
                    if val and not val.startswith("data:") and not _is_placeholder_src(val):
                        # 补全路径
                        if val.startswith("//"):
                            val = "https:" + val
                        elif not val.startswith(("http://", "https://")):
                            try:
                                val = urljoin(page_base, val)
                            except Exception:
                                continue
                        img["src"] = val
                        stats_count["lazy_fixed"] += 1
                        # 清理懒加载属性
                        for a in _LAZY_ATTRS:
                            if img.has_attr(a):
                                del img[a]
                        break

        # 同时处理那些 src 缺失但懒加载属性有值的 img
        for img in soup.find_all("img"):
            if img.get("src", "").strip():
                continue
            for attr in _LAZY_ATTRS:
                val = img.get(attr, "").strip()
                if val and not val.startswith("data:"):
                    if val.startswith("//"):
                        val = "https:" + val
                    elif not val.startswith(("http://", "https://")):
                        try:
                            val = urljoin(page_base, val)
                        except Exception:
                            continue
                    img["src"] = val
                    stats_count["lazy_fixed"] += 1
                    stats_count["total"] += 1
                    for a in _LAZY_ATTRS:
                        if img.has_attr(a):
                            del img[a]
                    break

        # ===== 第3层：CSS 背景图提取 =====
        bg_re = re.compile(r'url\(["\']?(.*?)["\']?\)', re.I)
        for el in list(soup.find_all(style=True)):
            style_text = el.get("style", "")
            if "background" not in style_text.lower():
                continue
            matches = bg_re.findall(style_text)
            for bg_url in matches:
                bg_url = bg_url.strip()
                if not bg_url or bg_url.startswith("data:"):
                    continue
                # 补全路径
                if bg_url.startswith("//"):
                    abs_url = "https:" + bg_url
                elif not bg_url.startswith(("http://", "https://")):
                    try:
                        abs_url = urljoin(page_base, bg_url)
                    except Exception:
                        continue
                else:
                    abs_url = bg_url
                # 检查是否已经有同 src 的 img 在该元素内
                existing = [i for i in el.find_all("img") if i.get("src") == abs_url]
                if existing:
                    continue
                # 插入新的 <img> 标签
                try:
                    new_img = soup.new_tag("img", src=abs_url,
                                           style="max-width:100%;height:auto;display:block;")
                    new_img["referrerpolicy"] = "no-referrer"
                    el.insert(0, new_img)
                    stats_count["css_extracted"] += 1
                    stats_count["total"] += 1
                except Exception:
                    pass

        # ===== 第3层追加：CSS 外部样式表背景图提取 =====
        for link in soup.find_all('link', rel='stylesheet'):
            css_url = link.get('href')
            if css_url:
                css_url = urljoin(page_base, css_url)
                try:
                    css_text = requests.get(css_url, timeout=5).text
                    for img_path in re.findall(r'url\(["\']?(.*?)["\']?\)', css_text):
                        if img_path.lower().endswith(('.jpg', '.png', '.gif', '.jpeg', '.webp')):
                            new_img = soup.new_tag('img', src=urljoin(css_url, img_path),
                                                   style='display:none;', **{'data-source': 'css-bg'})
                            if soup.body:
                                soup.body.append(new_img)
                            stats_count["css_extracted"] += 1
                            stats_count["total"] += 1
                except Exception:
                    pass

        # ===== 第3层追加：Swiper 轮播图 data-background 提取 =====
        for div in soup.find_all(attrs={"data-background": True}):
            bg_url = div.get('data-background')
            if bg_url and not bg_url.startswith('data:'):
                div.append(soup.new_tag('img', src=urljoin(page_base, bg_url),
                                        **{'data-source': 'swiper-bg'}))
                stats_count["css_extracted"] += 1
                stats_count["total"] += 1

        # ===== 第4层：防盗链绕过 =====
        head = soup.find("head")
        if head:
            # 检查是否已有 referrer meta
            existing_meta = head.find("meta", attrs={"name": "referrer"})
            if not existing_meta:
                meta_tag = soup.new_tag("meta")
                meta_tag["name"] = "referrer"
                meta_tag["content"] = "no-referrer"
                head.insert(0, meta_tag)
        else:
            # 没有 <head>，创建一个并插入到 <html> 最前面
            html_tag = soup.find("html")
            if html_tag:
                head_tag = soup.new_tag("head")
                meta_tag = soup.new_tag("meta")
                meta_tag["name"] = "referrer"
                meta_tag["content"] = "no-referrer"
                head_tag.insert(0, meta_tag)
                html_tag.insert(0, head_tag)

        # 同时给所有 img 添加 referrerpolicy 属性
        for img in soup.find_all("img"):
            if not img.get("referrerpolicy"):
                img["referrerpolicy"] = "no-referrer"

        # ===== 第5层：可选图片本地化 + Base64 内嵌 =====
        if cfg.IMAGE_DOWNLOAD:
            # 确定本地图片存储目录
            output_root = cfg.LOCAL_BACKUP_DIR
            parsed_root = urlparse(base_url or url)
            domain = parsed_root.netloc.replace("www.", "").replace(":", "_")
            # 使用与 multi_level_store_node 一致的逻辑
            default_backup = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
            if os.path.abspath(output_root) != os.path.abspath(default_backup):
                site_dir = output_root
            else:
                site_dir = os.path.join(output_root, domain)
            images_dir = os.path.join(site_dir, "images")
            os.makedirs(images_dir, exist_ok=True)

            # 随机 User-Agent 池
            _UA_POOL = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            ]
            import random
            import hashlib
            import base64

            # ★ 检查是否启用 Base64 内嵌模式
            _INLINE_MODE = getattr(cfg, 'IMAGE_INLINE_MODE', False)

            for img in soup.find_all("img"):
                src = img.get("src", "").strip()
                if not src or src.startswith("data:"):
                    continue
                if not src.startswith(("http://", "https://")):
                    continue

                # 生成本地文件名（URL 的 MD5 + 扩展名）
                try:
                    url_hash = hashlib.md5(src.encode("utf-8")).hexdigest()[:12]
                    # 尝试从 URL 提取扩展名
                    parsed_src = urlparse(src)
                    path_part = parsed_src.path.lower()
                    ext = ".jpg"  # 默认
                    for e in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"]:
                        if e in path_part:
                            ext = e
                            break
                    local_name = f"{url_hash}{ext}"
                    local_path = os.path.join(images_dir, local_name)

                    # 如果已存在且非 base64 模式 → 跳过下载
                    if not _INLINE_MODE and os.path.exists(local_path) and os.path.getsize(local_path) > 100:
                        img["src"] = os.path.join("images", local_name)
                        stats_count["downloaded"] += 1
                        continue

                    # 下载图片（★ 使用当前页面 URL 作为 Referer 防防盗链）
                    # ★ 如果页面 URL 不可用，回退到网站根域名（绝不使用空字符串）
                    _img_referer = url or base_url
                    if not _img_referer and src.startswith("http"):
                        # 最后兜底：从图片 URL 反推根域名
                        _img_referer = f"{urlparse(src).scheme}://{urlparse(src).netloc}/"
                    try:
                        dl_headers = {
                            "User-Agent": random.choice(_UA_POOL),
                            "Referer": _img_referer,
                        }
                        resp = requests.get(src, headers=dl_headers, timeout=10,
                                           verify=False, stream=True)
                        if resp.status_code == 200:
                            content = resp.content
                            if len(content) > 100:
                                # 始终保存到本地
                                with open(local_path, "wb") as f:
                                    f.write(content)
                                stats_count["downloaded"] += 1

                                if _INLINE_MODE:
                                    # ★ Base64 内嵌模式 + 大小限制
                                    _MAX_INLINE_SIZE = int(getattr(cfg, 'IMAGE_MAX_INLINE_SIZE', 2097152))
                                    if len(content) > _MAX_INLINE_SIZE:
                                        # 超过 2MB 不内嵌，保留本地路径
                                        img["src"] = os.path.join("images", local_name)
                                        print(f"  📦 [图片] 超过 {_MAX_INLINE_SIZE//1024//1024}MB 限制，保留本地路径: {src[:80]}")
                                    else:
                                        mime_type = {
                                            ".jpg": "image/jpeg",
                                            ".jpeg": "image/jpeg",
                                            ".png": "image/png",
                                            ".gif": "image/gif",
                                            ".webp": "image/webp",
                                            ".svg": "image/svg+xml",
                                            ".bmp": "image/bmp",
                                        }.get(ext, "image/jpeg")
                                        b64_data = base64.b64encode(content).decode("ascii")
                                        img["src"] = f"data:{mime_type};base64,{b64_data}"
                                else:
                                    img["src"] = os.path.join("images", local_name)
                            else:
                                stats_count["download_failed"] += 1
                        elif resp.status_code == 403 and _INLINE_MODE:
                            # 403 防盗链：尝试用根域名作为 Referer 重试（绝不使用空字符串）
                            _root_referer = f"{urlparse(src).scheme}://{urlparse(src).netloc}/"
                            try:
                                resp2 = requests.get(src, headers={
                                    "User-Agent": random.choice(_UA_POOL),
                                    "Referer": _root_referer,
                                }, timeout=10, verify=False, stream=True)
                                if resp2.status_code == 200 and len(resp2.content) > 100:
                                    content = resp2.content
                                    with open(local_path, "wb") as f:
                                        f.write(content)
                                    stats_count["downloaded"] += 1
                                    _MAX_INLINE_SIZE = int(getattr(cfg, 'IMAGE_MAX_INLINE_SIZE', 2097152))
                                    if len(content) > _MAX_INLINE_SIZE:
                                        img["src"] = os.path.join("images", local_name)
                                    else:
                                        mime_type = {
                                            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                                            ".png": "image/png", ".gif": "image/gif",
                                            ".webp": "image/webp", ".svg": "image/svg+xml",
                                            ".bmp": "image/bmp",
                                        }.get(ext, "image/jpeg")
                                        b64_data = base64.b64encode(content).decode("ascii")
                                        img["src"] = f"data:{mime_type};base64,{b64_data}"
                                else:
                                    stats_count["download_failed"] += 1
                            except Exception:
                                stats_count["download_failed"] += 1
                        else:
                            stats_count["download_failed"] += 1
                    except Exception:
                        stats_count["download_failed"] += 1
                except Exception:
                    stats_count["download_failed"] += 1

        # 将抢救后的 HTML 写回 state
        rescued_html = str(soup)
        current_page["raw_html"] = rescued_html
        current_page["image_rescue_stats"] = stats_count

        # 打印统计日志
        print(f"\n{'='*60}")
        print(f"🖼️ [图片抢救] 共发现 {stats_count['total']} 张图片，"
              f"补全路径 {stats_count['path_fixed']} 张，")
        print(f"  破解懒加载 {stats_count['lazy_fixed']} 张，"
              f"提取CSS背景 {stats_count['css_extracted']} 张，")
        print(f"  本地化下载 {stats_count['downloaded']} 张，"
              f"失败 {stats_count['download_failed']} 张")
        print(f"{'='*60}")

    except Exception as e:
        import traceback
        print(f"  [图片抢救异常] {e}")
        traceback.print_exc()
        # 抢救失败不影响流程，保留原始 HTML
        current_page["image_rescue_stats"] = stats_count

    return {
        **state,
        "current_page": current_page,
    }


# ======================================================================
# 🔴 需求 1：CSVWriter 单例类 — 生成符合公司标准的 CSV 文件
# ======================================================================

import csv
import uuid
import threading
from datetime import datetime

_CSV_WRITER_INSTANCE = None
_CSV_LOCK = threading.Lock()


def _generate_19_digit_id() -> str:
    """生成 19 位随机数字字符串作为 id"""
    return "".join([str(random.randint(0, 9)) for _ in range(19)])


def _generate_32_uuid() -> str:
    """生成 32 位随机字母数字作为 uuid"""
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join([random.choice(chars) for _ in range(32)])


def _now_str() -> str:
    """当前时间字符串 YYYY-MM-DD HH:MM:SS"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class CSVWriter:
    """
    单例 CSV 写入器，负责表头初始化和安全写入（使用 csv.QUOTE_ALL 防止 HTML 字段错乱）。

    CSV 表头（严格按顺序）：
    id,sys_platform,uuid,bstudio_create_time,gsmc,ywlx1,ywlx2,ywlx3,ywlx4,
    url,timestamp,riqi,title,html,zdr,download_link,img_url,img_title
    """

    _CSV_HEADER = [
        "id", "sys_platform", "uuid", "bstudio_create_time",
        "gsmc", "ywlx1", "ywlx2", "ywlx3", "ywlx4",
        "url", "timestamp", "riqi", "title", "html",
        "zdr", "download_link", "img_url", "img_title",
    ]

    def __init__(self, csv_dir: str, company_name: str):
        os.makedirs(csv_dir, exist_ok=True)
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', company_name)
        self.filepath = os.path.join(csv_dir, f"{safe_name}.csv")
        self._initialized = os.path.exists(self.filepath)
        self._file = open(self.filepath, "a", newline="", encoding="utf-8-sig")
        self._writer = csv.writer(self._file, quoting=csv.QUOTE_ALL)
        if not self._initialized:
            self._writer.writerow(self._CSV_HEADER)
            self._file.flush()
        self._lock = threading.Lock()

    def write_row(self, row_data: Dict[str, str]) -> None:
        """
        安全写入一行 CSV 数据。
        row_data 必须包含 _CSV_HEADER 对应的键。
        HTML 字段由 csv.QUOTE_ALL 自动处理转义。
        """
        with self._lock:
            row = [row_data.get(col, "") for col in self._CSV_HEADER]
            self._writer.writerow(row)
            self._file.flush()

    def close(self) -> None:
        if self._file:
            self._file.close()

    def __del__(self):
        self.close()


def _get_csv_writer(company_name: str) -> CSVWriter:
    """获取或创建 CSVWriter 实例"""
    global _CSV_WRITER_INSTANCE
    with _CSV_LOCK:
        if _CSV_WRITER_INSTANCE is None:
            csv_dir = os.path.join(os.getcwd(), config.CSV_OUTPUT_DIR)
            _CSV_WRITER_INSTANCE = CSVWriter(csv_dir, company_name)
        return _CSV_WRITER_INSTANCE


def _reset_csv_writer():
    """重置 CSVWriter 实例（切换公司时使用）"""
    global _CSV_WRITER_INSTANCE
    with _CSV_LOCK:
        if _CSV_WRITER_INSTANCE:
            _CSV_WRITER_INSTANCE.close()
            _CSV_WRITER_INSTANCE = None


# ======================================================================
# 🔴 需求 2：render_clean_html — 将提取的正文包装成带 CSS 的 HTML 字符串
# ======================================================================

def render_clean_html(raw_content: str, title: str, url: str = "", riqi: str = "") -> str:
    """
    将提取出的正文内容嵌入到统一的 HTML 模板中。
    自带基础 CSS 排版，使用 <article class="render-page"> 包裹。

    参数:
      raw_content: 已清洗的正文 HTML 片段（可包含 img 标签）
      title:       页面标题
      url:         原始 URL
      riqi:        发布时间

    返回:
      完整的 HTML 文档字符串
    """
    title_block = f"<h1>{title}</h1>" if title else ""
    time_block = f'<p class="publish-time">发布时间：{riqi}</p>' if riqi else ""
    url_comment = f"<!-- url：{url} -->" if url else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="referrer" content="no-referrer">
<title>{title or url}</title>
<style>{_UNIFIED_CSS}</style>
</head>
<body>
{url_comment}
<article class="render-page">
<div class="content-wrapper">
{title_block}
{time_block}
{raw_content}
</div>
</article>
</body>
</html>"""


# ======================================================================
# 🔴 需求 3：get_nav_name — 获取 1-4 级导航栏名称列表
# ======================================================================

# URL路径到导航名称的通用映射（兜底用，优先使用 DOM 提取的面包屑）
_URL_PATH_NAV_MAP = {
    "/about": "关于我们",
    "/aboutus": "关于我们",
    "/about-us": "关于我们",
    "/gywm": "关于我们",
    "/news": "新闻中心",
    "/xwzx": "新闻中心",
    "/product": "产品中心",
    "/cpzx": "产品中心",
    "/business": "业务介绍",
    "/ywjs": "业务介绍",
    "/news/company": "公司新闻",
    "/news/industry": "行业新闻",
    "/notice": "通知公告",
    "/gsgg": "公示公告",
    "/tender": "招标公告",
    "/zbgg": "招标公告",
    "/recruit": "人才招聘",
    "/rczp": "人才招聘",
    "/contact": "联系我们",
    "/lxwm": "联系我们",
    "/culture": "企业文化",
    "/qywh": "企业文化",
}


def get_nav_name(url: str, soup: BeautifulSoup = None, breadcrumb: List[str] = None,
                 nav_mapping: Dict[str, str] = None, base_url: str = "") -> List[str]:
    """
    获取当前 URL 所属的 1-4 级导航栏名称列表。

    优先级：
      1. 页面 DOM 中的面包屑导航（最准确）
      2. nav_mapping URL路径映射 + 面包屑补充
      3. _URL_PATH_NAV_MAP 兜底映射
      4. 页面 <title> 标签提取

    参数:
      url:         当前抓取的原始 URL
      soup:        页面 BeautifulSoup 对象（可选）
      breadcrumb:  已提取的面包屑列表（可选）
      nav_mapping: 一级导航映射 {名称: URL前缀}
      base_url:    base URL

    返回:
      [ywlx1, ywlx2, ywlx3, ywlx4] — 固定 4 个元素，不足留空
    """
    result = ["", "", "", ""]

    # 1. 优先使用 DOM 面包屑
    if breadcrumb and len(breadcrumb) > 0:
        for i, crumb in enumerate(breadcrumb[:4]):
            result[i] = re.sub(r'\s+', ' ', crumb.strip())
        return result

    # 2. 如果提供了 soup，尝试实时提取面包屑
    if soup is not None:
        try:
            live_breadcrumb = _parse_breadcrumb(soup)
            if live_breadcrumb:
                for i, crumb in enumerate(live_breadcrumb[:4]):
                    result[i] = re.sub(r'\s+', ' ', crumb.strip())
                return result
        except Exception:
            pass

    # 3. 使用 nav_mapping 映射
    if nav_mapping and base_url:
        try:
            category = _get_category_name(url, base_url, nav_mapping)
            if category and category != "其他页面":
                result[0] = category
        except Exception:
            pass

    # 4. URL path 兜底映射
    if result[0] == "":
        try:
            parsed = urlparse(url)
            path = parsed.path.rstrip("/")
            path_parts = [p for p in path.split("/") if p]
            for i, part in enumerate(path_parts[:4]):
                result[i] = part
        except Exception:
            pass

    # 5. 从 soup title 提取作为最后一级
    if soup is not None and all(v == "" for v in result):
        try:
            title_tag = soup.find("title")
            if title_tag:
                title_text = title_tag.get_text(strip=True)
                parts = re.split(r'[-|_—–]', title_text)
                if parts:
                    result[0] = parts[0].strip()[:20]
        except Exception:
            pass

    return result


# ======================================================================
# 🔴 需求 4：重写 multi_level_store_node — 整合 CSV 写入 + 按导航栏目录保存 HTML
# ======================================================================

def multi_level_store_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    存储节点（重写版）：
    1. 为每个结果写入 CSV 行（使用 CSVWriter）
    2. 按导航栏目录分类保存本地 HTML 文件
       - 目录结构：./data/{导航栏名称}/ 或 ./{公司名}/{导航栏名称}/
       - 文件名格式：{导航栏}_{标题}.html

    路径规则：
      1. 使用 get_nav_name() 获取导航名称
      2. 目录名优先使用 ywlx1（一级导航），不足用分类兜底
      3. 文件名 = 导航栏名_标题，非法字符替换为下划线
    """
    results = state.get("results", [])
    root_url = state.get("root_url", "")
    base_url = state.get("base_url", root_url)
    nav_mapping = dict(state.get("nav_mapping", {}))
    stats = dict(state.get("stats", {"total": 0, "success": 0, "failed": 0, "skipped": 0, "saved": 0}))

    print(f"\n{'='*60}")
    print(f"[存储] 共 {len(results)} 个页面，导航栏分类模式 + CSV 输出")
    if nav_mapping:
        print(f"  📋 导航分类: {list(nav_mapping.keys())}")
    print(f"{'='*60}")

    if not results:
        print("  [存储] 无结果可保存")
        return {**state, "stats": stats}

    # ★ 获取公司名称
    first_url = results[0].get("url", "") if results else ""
    company_name = get_current_company(first_url) if first_url else "未知公司"

    # ★ 初始化 CSV Writer
    csv_writer = _get_csv_writer(company_name)

    # ★ 基础输出目录路径（延迟创建：只有真正保存文件时才创建目录）
    output_root = os.path.join(os.getcwd(), company_name)
    _output_root_created = False  # 标记根目录是否已创建

    saved = 0

    for idx, page in enumerate(results):
        url = page.get("url", "")
        html = page.get("html", "")
        title = page.get("title", "")
        breadcrumb = page.get("breadcrumb", [])
        depth = page.get("depth", 1)
        riqi = page.get("riqi", "")

        # 解析已清洗的 HTML 获取 soup
        soup = BeautifulSoup(html, "html.parser") if html else BeautifulSoup("", "html.parser")

        # 1. 获取导航栏名称列表 (1-4级)
        nav_list = get_nav_name(url, soup=soup, breadcrumb=breadcrumb,
                                nav_mapping=nav_mapping, base_url=base_url)
        ywlx1, ywlx2, ywlx3, ywlx4 = nav_list[0], nav_list[1], nav_list[2], nav_list[3]

        # 2. 生成唯一 ID 和时间戳
        row_id = _generate_19_digit_id()
        row_uuid = _generate_32_uuid()
        now_ts = _now_str()

        # 3. 生成渲染后的 HTML（用于 CSV 的 html 字段）
        #    从已清洗的 HTML 中提取正文内容
        content_soup = BeautifulSoup(html, "html.parser")
        content_wrapper = content_soup.find("div", class_="content-wrapper")
        if content_wrapper:
            raw_content = str(content_wrapper)
        else:
            body = content_soup.find("body")
            raw_content = str(body) if body else html

        csv_html = render_clean_html(raw_content, title, url=url, riqi=riqi)

        # 4. 写入 CSV 行
        row_data = {
            "id": row_id,
            "sys_platform": config.SYS_PLATFORM,
            "uuid": row_uuid,
            "bstudio_create_time": now_ts,
            "gsmc": company_name,
            "ywlx1": ywlx1,
            "ywlx2": ywlx2,
            "ywlx3": ywlx3,
            "ywlx4": ywlx4,
            "url": url,
            "timestamp": now_ts,
            "riqi": riqi,
            "title": title,
            "html": csv_html,
            "zdr": "",
            "download_link": "",
            "img_url": "",
            "img_title": "",
        }

        try:
            csv_writer.write_row(row_data)
            print(f"  📊 [CSV] 已写入: {title[:30]} | {ywlx1} > {ywlx2} > {ywlx3} > {ywlx4}")
        except Exception as e:
            print(f"  ❌ [CSV写入失败] {title[:30]}: {e}")

        # 5. 本地 HTML 文件按导航栏目录保存
        #    目录名：优先 ywlx1（一级导航），降级用分类函数
        if ywlx1 and ywlx1.strip():
            nav_dir_name = re.sub(r'[\\/:*?"<>|]', '_', ywlx1.strip())
        else:
            # 降级到旧的分类函数
            tmp_soup = BeautifulSoup(html, "html.parser")
            nav_dir_name = determine_category(tmp_soup, url)
            nav_dir_name = re.sub(r'[\\/:*?"<>|]', '_', nav_dir_name)

        if not nav_dir_name or nav_dir_name.strip() == "":
            nav_dir_name = "其他"

        # 构建目标目录
        target_dir = os.path.join(output_root, nav_dir_name)
        os.makedirs(target_dir, exist_ok=True)

        # 生成文件名：导航栏名_标题
        safe_title = re.sub(r'[\\/:*?"<>|]', '_', title) if title else "未命名"
        if len(safe_title) > 40:
            safe_title = safe_title[:40]

        if ywlx1 and ywlx1.strip():
            file_name = f"{ywlx1}_{safe_title}.html"
        else:
            file_name = f"{safe_title}.html"

        # 再次清理整个文件名
        file_name = re.sub(r'[\\/:*?"<>|]', '_', file_name)

        save_path = os.path.join(target_dir, file_name)

        # 处理重名
        counter = 1
        while os.path.exists(save_path):
            dir_path, fname = os.path.split(save_path)
            name_no_ext, ext = os.path.splitext(fname)
            save_path = os.path.join(dir_path, f"{name_no_ext}_{counter}{ext}")
            counter += 1

        try:
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"  📁 [归档] {url[:60]} → {save_path}")
            saved += 1
        except Exception as e:
            print(f"  [{idx+1}/{len(results)}] ❌ 保存失败: {e}")

    stats["saved"] = saved
    print(f"\n📁 所有文件保存至: {output_root}")
    print(f"   CSV 输出: {csv_writer.filepath}")
    print(f"   成功保存: {saved}/{len(results)} 个 HTML 文件")

    # 关闭 CSVWriter（不删除，后续可能追加）
    # 注意：多批次调用时保持 CSV 文件打开

    return {**state, "stats": stats}


# ======================================================================
# 🔴 需求 2：fallback_node — 致命错误兜底节点 (Phase 1)
# ======================================================================

def fallback_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    兜底节点：当任意核心节点连续失败 ≥3 次时，路由到此节点。
    记录致命错误日志，优雅结束当前分支，而非让整个图崩溃。

    Partial Update 返回：
      - fatal_error: True（保持以阻止后续循环）
      - error_log: 追加致命错误记录
    """
    error_log = list(state.get("error_log", []))
    failures = dict(state.get("node_consecutive_failures", {}))
    current_url = state.get("current_url", "unknown")

    fatal_entry = {
        "timestamp": datetime.now().isoformat(),
        "node": "fallback",
        "url": current_url,
        "error_type": "FATAL_CONSECUTIVE_FAILURES",
        "message": f"节点连续失败达到上限，失败计数: {failures}",
    }
    error_log.append(fatal_entry)

    agent_logger.error(
        f"[fallback_node] 致命错误回退 | url={current_url[:80]} | "
        f"failures={failures} | total_errors={len(error_log)}"
    )

    print(f"\n{'='*60}")
    print(f"🛑 [FALLBACK] 致命错误！连续失败次数超限，优雅终止当前目标。")
    print(f"   失败详情: {failures}")
    print(f"   累计错误: {len(error_log)} 条")
    print(f"{'='*60}")

    return {
        "fatal_error": True,
        "error_log": error_log,
        "node_consecutive_failures": failures,
    }
