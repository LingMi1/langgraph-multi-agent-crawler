"""
LangChain @tool 工具封装 (Phase 2: ReAct Agent 工具集)

将现有的 fetch/extract 逻辑封装为 @tool，供 LLM Agent 通过 Function Calling 调用。

工具列表:
  1. fetch_page         — 抓取单个页面 HTML
  2. extract_links      — 提取页面所有同域链接
  3. clean_and_extract  — 清洗 HTML + 提取结构化数据（Pydantic 校验）
  4. save_data          — 保存已提取的数据到 CSV + HTML 文件
  5. finish_task        — 结束当前任务，输出汇总
"""

import os
import re
import json
import time
import hashlib
import random
import tempfile
import requests
import urllib3
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

from langchain.tools import tool
from schemas import (
    AgentState,
    NewsArticleSchema,
    ValidationResult,
    agent_logger,
)
from memory import UrlMemory
import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 复用 nodes.py 中的核心逻辑（避免重复代码）
import nodes as _nodes

# 全局记忆库实例
url_memory = UrlMemory()

# Playwright 懒加载（避免必须安装）
_PLAYWRIGHT_AVAILABLE = None
_BROWSER = None


def _ensure_playwright():
    """
    懒加载 Playwright 浏览器实例（单例 + 首次使用才初始化），带超时保护。
    
    ★ 修复：首次启动失败后不再永久禁用，允许后续重试（处理临时性启动异常）。
    import 失败才永久标记为不可用（说明根本没装）。
    """
    global _PLAYWRIGHT_AVAILABLE, _BROWSER
    if _PLAYWRIGHT_AVAILABLE is None:
        try:
            from playwright.sync_api import sync_playwright
            _PLAYWRIGHT_AVAILABLE = True
        except ImportError:
            _PLAYWRIGHT_AVAILABLE = False
            agent_logger.warning("[Playwright] 未安装 playwright，将使用 requests 静态抓取。安装: pip install playwright && playwright install chromium")
    if _PLAYWRIGHT_AVAILABLE and _BROWSER is None:
        import threading
        _launch_result = [None]     # 存储启动结果
        _launch_exception = [None]  # 存储启动异常

        def _do_launch():
            try:
                from playwright.sync_api import sync_playwright
                print("[Playwright] ⏳ 正在启动 Chromium 无头浏览器（首次启动约 5-15 秒）...")
                _pw = sync_playwright().start()
                browser = _pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                _launch_result[0] = browser
                print("[Playwright] ✅ Chromium 无头浏览器启动成功")
            except Exception as e:
                _launch_exception[0] = e

        _t = threading.Thread(target=_do_launch, daemon=True)
        _t.start()
        _t.join(timeout=30)  # 最多等 30 秒

        if _launch_exception[0] is not None:
            err = _launch_exception[0]
            agent_logger.warning(f"[Playwright] 启动浏览器异常: {err}，本次降级使用 requests（下个请求会重试）")
            print(f"[Playwright] ❌ 浏览器启动异常: {err}，本次已降级为 requests（下个页面会重试启动）")
            # ★ 不再永久置 False：保留 _PLAYWRIGHT_AVAILABLE=True，下次 _ensure_playwright 会重试启动
        elif _launch_result[0] is None:
            agent_logger.warning("[Playwright] 浏览器启动超时（>30 秒），本次降级使用 requests（下个请求会重试）")
            print("[Playwright] ❌ 浏览器启动超时（>30 秒），本次已降级为 requests（下个页面会重试启动）")
            # ★ 同上：不永久禁用
        else:
            _BROWSER = _launch_result[0]
            agent_logger.info("[Playwright] Chromium 无头浏览器已启动")
    return _PLAYWRIGHT_AVAILABLE


def _fetch_with_playwright(url: str) -> Tuple[Optional[str], int, str]:
    """使用 Playwright 无头浏览器抓取 JS 渲染后的 HTML"""
    try:
        _ensure_playwright()
        if not _PLAYWRIGHT_AVAILABLE or not _BROWSER:
            return None, 0, "Playwright 不可用"

        page = _BROWSER.new_page()
        try:
            page.set_default_timeout(30000)
            page.set_extra_http_headers({
                "User-Agent": config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            response = page.goto(url, wait_until="networkidle", timeout=30000)
            http_status = response.status if response else 0

            # 额外等待动态内容渲染
            page.wait_for_timeout(2000)

            # 尝试滚动到底部触发懒加载
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)
            except Exception:
                pass

            html = page.content()
            return html, http_status, ""
        finally:
            page.close()
    except Exception as e:
        return None, 0, f"Playwright: {type(e).__name__}: {str(e)[:200]}"


@tool
def fetch_page(url: str) -> str:
    """
    抓取指定 URL 的 HTML 内容。内部使用 tenacity 指数退避重试（最多3次）。
    首次抓取尝试 Playwright 动态渲染，失败则降级为静态 requests 抓取。

    参数:
      url: 要抓取的页面 URL（必须是完整的 http/https URL）

    返回:
      JSON 字符串: {"success": bool, "url": str, "html_length": int, "title": str, "http_status": int, "error": str}
    """
    agent_logger.info(f"[Tool] fetch_page | url={url[:100]}")

    # ★ 长期记忆检查：如果 URL 已访问且有缓存的 HTML，直接返回缓存内容
    if url_memory.is_visited(url):
        cached_html = url_memory.get_cached_html(url)
        if cached_html:
            agent_logger.info(f"[Tool] fetch_page | [fetch_page] 命中记忆缓存，返回缓存HTML len={len(cached_html)}")
            # 提取标题
            try:
                soup = BeautifulSoup(cached_html, "html.parser")
                title = soup.title.string.strip() if soup.title and soup.title.string else ""
            except Exception:
                title = ""
            # ★ DIAG: 缓存 HTML 过短时也 dump
            if len(cached_html) < 300:
                print(f"[DIAG] fetch_page 命中的缓存 HTML 过短 ({len(cached_html)} 字节) | url={url[:80]}")
                print(f"[DIAG] 缓存 HTML 前300字符: {cached_html[:300]}")
            return json.dumps({
                "success": True,
                "url": url,
                "html_length": len(cached_html),
                "title": title,
                "http_status": 0,
                "error": "",
                "suggestion": "",
                "fetch_method": "memory_cache",
            }, ensure_ascii=False)
        else:
            # 已访问但无缓存 HTML（预检写入或之前抓取失败），允许重新抓取一次
            agent_logger.info(f"[Tool] fetch_page | URL 已记录但无缓存HTML，允许重新抓取: {url[:80]}")

    html = None
    http_status = 0
    error_msg = ""
    fetch_method = "requests"

    # ★ DIAG: 打印 Playwright 可用状态
    pw_available = _PLAYWRIGHT_AVAILABLE if _PLAYWRIGHT_AVAILABLE is not None else "未检测"
    print(f"[DIAG] fetch_page Playwright 可用状态={pw_available} | url={url[:80]}")

    # ★ 1) 优先尝试 Playwright 无头浏览器（JS 动态渲染站必备）
    pw_html, pw_status, pw_err = _fetch_with_playwright(url)
    if pw_html and len(pw_html) > 200:
        html = pw_html
        http_status = pw_status
        fetch_method = "playwright"
        agent_logger.info(f"[Tool] fetch_page | Playwright 成功 | len={len(pw_html)}")
    elif pw_html:
        print(f"[DIAG] fetch_page Playwright 返回 {len(pw_html)} 字节（阈值=200），暂存待比较")
    if not html:
        agent_logger.info(f"[Tool] fetch_page | Playwright 未生成有效 HTML，降级到静态 requests 抓取")

    # ★ 2) 降级：静态 requests 抓取
    if not html:
        try:
            from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
            import logging
            import requests.adapters
            from urllib3.util.retry import Retry

            def _get_session():
                session = requests.Session()
                retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
                session.mount('http://', requests.adapters.HTTPAdapter(max_retries=retries))
                session.mount('https://', requests.adapters.HTTPAdapter(max_retries=retries))
                return session

            _USER_AGENTS = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
            ]

            @retry(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=2, max=15),
                retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError, requests.HTTPError)),
                before_sleep=before_sleep_log(agent_logger, logging.WARNING),
                reraise=True,
            )
            def _do_fetch(target_url: str) -> requests.Response:
                session = _get_session()
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
                if 400 <= resp.status_code < 500 and resp.status_code not in (429,):
                    return resp
                if resp.status_code == 429:
                    raise requests.HTTPError("429 Too Many Requests", response=resp)
                resp.raise_for_status()
                return resp

            resp = _do_fetch(url)
            http_status = resp.status_code
            if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "latin-1"):
                resp.encoding = resp.apparent_encoding or "utf-8"
            html = resp.text
            fetch_method = "requests"
            agent_logger.info(f"[Tool] fetch_page | requests 成功 | len={len(html)}")
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)[:200]}"
            agent_logger.warning(f"[Tool] fetch_page 失败 | url={url[:80]} | error={error_msg}")

    # ★ 3) 双向比较：如果 Playwright 和 requests 都拿到了内容，取更长的那份
    if html and pw_html and len(pw_html) > len(html):
        print(f"[DIAG] fetch_page 切换到 Playwright 结果: Playwright={len(pw_html)} > requests={len(html)}")
        html = pw_html
        http_status = pw_status
        fetch_method = "playwright"

    # ★ 4) DIAG 诊断日志（增强：HTML<300 时 dump 前 300 字符到日志）
    diag_html_len = len(html) if html else 0
    if diag_html_len == 0:
        print(f"[DIAG] 情况A: fetch_page 完全失败 | url={url[:80]} | method={fetch_method}")
    elif diag_html_len < 300:
        print(f"[DIAG] 情况B: fetch_page 成功但 HTML 过短 ({diag_html_len} 字节) | url={url[:80]} | method={fetch_method}")
        # ★ 诊断增强: dump 前 300 字符，让用户肉眼确认是 404 页还是空壳
        if html:
            print(f"[DIAG] HTML 前300字符: {html[:300]}")
    elif diag_html_len < 500:
        print(f"[DIAG] 情况C: fetch_page 成功但 HTML 偏短 ({diag_html_len} 字节) | url={url[:80]} | method={fetch_method}")
    else:
        print(f"[DIAG] 情况D: fetch_page 成功 | html_len={diag_html_len} | method={fetch_method}")

    if html:
        # 提取标题
        try:
            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.string.strip() if soup.title and soup.title.string else ""
        except Exception:
            title = ""
        # ★ 长期记忆：仅当 HTML 内容充足时才缓存（防止短 HTML 毒化缓存）
        if len(html) >= 500:
            url_memory.mark_visited(url, status="success", title=title, html_content=html)
        else:
            # 短 HTML 只标记 visited 但不缓存内容，下次调用会重新抓取
            url_memory.mark_visited(url, status="visited_short", title=title, html_content="")
            print(f"[DIAG] fetch_page HTML 过短({len(html)}字节)，不缓存内容，下次可重新抓取")

        # ★ 当 HTML 过短时生成诊断建议
        suggestion = ""
        if len(html) < 500:
            suggestion = (
                f"页面内容过短({len(html)}字节)，可能是JS渲染页面。"
                f"当前抓取方式={fetch_method}。"
                f"如未安装Playwright请执行: pip install playwright && playwright install chromium"
            )
            print(f"[DIAG] fetch_page 建议: {suggestion}")

        return json.dumps({
            "success": True,
            "url": url,
            "html_length": len(html),
            "title": title,
            "http_status": http_status,
            "error": "",
            "suggestion": suggestion,
            "fetch_method": fetch_method,
        }, ensure_ascii=False)
    else:
        # ★ 增强错误反馈：给出具体建议
        suggestion = ""
        if "403" in error_msg or "Cloudflare" in error_msg or "WAF" in error_msg:
            suggestion = "网站有反爬机制（403/Cloudflare/WAF），跳过此URL，尝试其他页面"
        elif "timeout" in error_msg.lower():
            suggestion = "网络超时，可重试1次或跳过"
        elif "SSL" in error_msg or "Certificate" in error_msg:
            suggestion = "SSL证书问题，跳过此URL"
        else:
            suggestion = "抓取失败，跳过此URL"
        
        # ★ 抓取失败只标记状态，不存 HTML 内容（避免后续命中的记忆缓存返回空内容）
        url_memory.mark_visited(url, status="failed", title="", html_content="")
        return json.dumps({
            "success": False,
            "url": url,
            "html_length": 0,
            "title": "",
            "http_status": http_status,
            "error": error_msg,
            "suggestion": suggestion,
            "fetch_method": fetch_method,
        }, ensure_ascii=False)


@tool
def extract_links(url: str, html: str) -> str:
    """
    从 HTML 内容中提取所有同域内部链接（去除 CSS/JS/图片等非页面链接）。
    自动跳过已访问的 URL（查询长期记忆）。

    ★ 如果传入的 html 太短（<500 字符），自动用 Playwright/requests 重新抓取。

    参数:
      url: 当前页面 URL（用于相对路径解析和域判断）
      html: 页面 HTML 源码字符串

    返回:
      JSON 字符串: {"total_found": int, "new_links": [...], "skipped_visited": int}
    """
    # ★ 兜底：如果 html 太短，重新抓取
    _orig_html_len = len(html) if html else 0
    if _orig_html_len < 500:
        agent_logger.warning(f"[Tool] extract_links | html 过短 ({_orig_html_len} 字节)，重新抓取: {url[:80]}")
        pw_html, pw_status, pw_err = _fetch_with_playwright(url)
        if pw_html and len(pw_html) > 500:
            html = pw_html
        else:
            try:
                import requests as req
                resp = req.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=15, verify=False)
                if resp.status_code < 500:
                    html = resp.text
            except Exception:
                pass

    agent_logger.info(f"[Tool] extract_links | url={url[:80]} | html_len={len(html) if html else 0}")
    try:
        soup = BeautifulSoup(html, "html.parser") if html else BeautifulSoup("", "html.parser")
        parsed_base = urlparse(url)
        base_domain = parsed_base.netloc.lower()

        all_links = []
        body = soup.find("body") or soup

        for a in body.find_all("a", href=True):
            href = a["href"].strip()
            if not href:
                continue
            # 过滤不可抓取链接
            if href.startswith(("javascript:", "mailto:", "tel:", "#", "ftp:")):
                continue
            abs_url = urljoin(url, href)
            parsed = urlparse(abs_url)
            # 同域检查
            if parsed.netloc.lower() != base_domain:
                continue
            # 排除非 HTML 后缀
            lower_path = parsed.path.lower()
            skip_exts = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
                        ".css", ".js", ".json", ".xml", ".pdf", ".doc", ".docx",
                        ".xls", ".xlsx", ".zip", ".rar", ".mp3", ".mp4", ".avi")
            if lower_path.endswith(skip_exts):
                continue
            # 标准化
            norm = urlunparse((parsed.scheme, parsed.netloc.lower(),
                              parsed.path.rstrip("/") or "/",
                              parsed.params, parsed.query, ""))
            text = a.get_text(strip=True)[:50]
            all_links.append({"url": abs_url, "text": text, "normalized": norm})

        # 去重 + 长期记忆过滤
        new_links = []
        seen_norms = set()
        skipped = 0
        for link in all_links:
            norm = link["normalized"]
            if norm in seen_norms:
                continue
            seen_norms.add(norm)
            # ★ 允许重试之前抓取失败的 URL（status=failed 或 visited_short）
            cached_html = url_memory.get_cached_html(link["url"])
            if cached_html and len(cached_html) > 0:
                # 有成功缓存的 HTML，跳过
                skipped += 1
                continue
            # 如果 URL 存在但没有缓存 HTML（之前失败了），允许重新处理
            # 不再简单地基于 is_visited 跳过
            new_links.append({"url": link["url"], "text": link["text"]})

        result = {
            "total_found": len(new_links),
            "new_links": new_links[:100],  # 最多返回100条，避免 token 爆炸
            "skipped_visited": skipped,
        }
        agent_logger.info(f"[Tool] extract_links | found={len(new_links)} new, skipped={skipped} visited")
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        agent_logger.error(f"[Tool] extract_links 失败 | {type(e).__name__}: {e}")
        return json.dumps({"total_found": 0, "new_links": [], "skipped_visited": 0, "error": str(e)[:200]},
                         ensure_ascii=False)


@tool
def clean_and_extract(url: str, html: str) -> str:
    """
    清洗页面 HTML（去头去尾去侧边栏 + 图片绝对路径），提取结构化数据，
    并使用 Pydantic NewsArticleSchema 进行校验。

    ★ 如果传入的 html 太短（<500 字符），自动用 Playwright/requests 重新抓取。

    参数:
      url: 页面 URL
      html: 原始 HTML 源码（可为空字符串，内部会重新抓取）

    返回:
      JSON 字符串: {"success": bool, "article": {...}, "validation_error": str}
    """
    # ★ 兜底：如果 html 太短，重新抓取
    _orig_html_len = len(html) if html else 0
    if _orig_html_len < 500:
        agent_logger.warning(f"[Tool] clean_and_extract | html 过短 ({_orig_html_len} 字节)，重新抓取: {url[:80]}")
        # 先尝试 Playwright
        pw_html, pw_status, pw_err = _fetch_with_playwright(url)
        if pw_html and len(pw_html) > 500:
            html = pw_html
            agent_logger.info(f"[Tool] clean_and_extract | Playwright 重新抓取成功 | len={len(html)}")
        else:
            # 降级到 requests
            try:
                import requests as req
                resp = req.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=15, verify=False)
                if resp.status_code < 500:
                    html = resp.text
                    agent_logger.info(f"[Tool] clean_and_extract | requests 重新抓取成功 | len={len(html)}")
            except Exception as e:
                agent_logger.warning(f"[Tool] clean_and_extract | 重新抓取也失败: {e}")

    agent_logger.info(f"[Tool] clean_and_extract | url={url[:80]} | html_len={len(html) if html else 0}")
    try:
        soup = BeautifulSoup(html, "html.parser")

        # 去噪音
        for tag in soup(["script", "style", "noscript", "iframe", "link"]):
            tag.decompose()

        # 去头去尾
        _nodes._remove_header_footer(soup)

        # 去侧边栏
        _nodes._remove_sidebar_ads_popups(soup)

        # 寻找正文
        content = _nodes._find_content_area(soup)
        if not content:
            content = soup.find("body") or soup

        # 图片绝对化
        img_count = _nodes._fix_image_srcs_absolute(content, url)

        # 提取标题
        title = _nodes._extract_page_title(soup)

        # 提取发布时间
        riqi = _nodes._extract_publish_time(soup)

        # 提取面包屑
        breadcrumb = _nodes._parse_breadcrumb(soup)

        # 构建 HTML 片段
        body_content = str(content)
        title_str = title or "未知标题"

        # Pydantic 校验
        try:
            article = NewsArticleSchema(
                url=url,
                title=title_str,
                publish_time=riqi if riqi else None,
                breadcrumb=breadcrumb,
                html_content=body_content[:50000],  # 截断避免过大
                images_count=img_count,
                nav_levels=["", "", "", ""],
            )
            NewsArticleSchema.model_validate(article.model_dump())
            agent_logger.info(f"[Tool] clean_and_extract | Pydantic 校验通过 | title={title_str[:40]}")
            return json.dumps({
                "success": True,
                "article": article.model_dump(),
                "validation_error": ""
            }, ensure_ascii=False)
        except Exception as val_err:
            agent_logger.warning(f"[Tool] clean_and_extract | Pydantic 校验失败 | reason={str(val_err)[:100]}")
            return json.dumps({
                "success": False,
                "article": None,
                "validation_error": f"Pydantic Validation Error: {str(val_err)[:300]}"
            }, ensure_ascii=False)

    except Exception as e:
        agent_logger.error(f"[Tool] clean_and_extract 异常 | {type(e).__name__}: {str(e)[:200]}")
        return json.dumps({
            "success": False,
            "article": None,
            "validation_error": f"{type(e).__name__}: {str(e)[:200]}"
        }, ensure_ascii=False)


@tool
def save_data(data_json: str) -> str:
    """
    将提取的文章数据保存到 CSV 文件和本地 HTML 目录。
    如果单次保存数据量超过 1000 条，将触发 HITL 人工介入中断。

    参数:
      data_json: JSON 字符串，格式: {"company_name": str, "articles": [...]}
                 articles 中每项包含: url, title, riqi, html, breadcrumb, ...
                 或直接传入已校验的 NewsArticleSchema dict

    返回:
      JSON: {"saved_count": int, "csv_path": str, "output_dir": str, "hitl_required": bool}
    """
    from nodes import _get_csv_writer, render_clean_html, get_current_company

    agent_logger.info(f"[Tool] save_data | 准备保存数据")

    try:
        data = json.loads(data_json)
        company_name = data.get("company_name", "未知公司")
        articles = data.get("articles", [])

        if not articles:
            return json.dumps({"saved_count": 0, "csv_path": "", "output_dir": "", "hitl_required": False},
                             ensure_ascii=False)

        # HITL 检查：单次保存超过 1000 条
        hitl_required = len(articles) > 1000

        # 初始化 CSV Writer
        csv_writer = _get_csv_writer(company_name)
        output_root = os.path.join(os.getcwd(), company_name)
        os.makedirs(output_root, exist_ok=True)

        saved = 0
        for article in articles:
            url = article.get("url", "")
            title = article.get("title", "未知标题")
            riqi = article.get("publish_time", article.get("riqi", ""))
            html = article.get("html_content", article.get("html", ""))
            breadcrumb = article.get("breadcrumb", [])
            try:
                # 生成完整的 HTML 文档
                rendered_html = render_clean_html(html, title, url=url, riqi=riqi)

                # 写入 CSV
                row_data = {
                    "id": "".join([str(random.randint(0, 9)) for _ in range(19)]),
                    "sys_platform": config.SYS_PLATFORM,
                    "uuid": "".join([random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(32)]),
                    "bstudio_create_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "gsmc": company_name,
                    "ywlx1": "", "ywlx2": "", "ywlx3": "", "ywlx4": "",
                    "url": url,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "riqi": riqi or "",
                    "title": title,
                    "html": rendered_html,
                    "zdr": "", "download_link": "", "img_url": "", "img_title": "",
                }
                csv_writer.write_row(row_data)
                saved += 1

                # ★ 独立保存 HTML 文件到磁盘
                try:
                    # 生成安全文件名
                    safe_title = title if title and title != "未知标题" else ""
                    safe_title = safe_title or "untitled_page"
                    safe_title = re.sub(r'[\\/:*?"<>|\r\n]+', '_', safe_title)[:80]
                    if not safe_title:
                        # 降级：从 URL 路径末段提取文件名
                        parsed_url = urlparse(url)
                        path_last = parsed_url.path.strip("/").split("/")[-1]
                        if path_last:
                            safe_title = re.sub(r'[\\/:*?"<>|\r\n]+', '_', os.path.splitext(path_last)[0])[:80]
                    if not safe_title:
                        safe_title = "page"
                    if len(safe_title) > 40:
                        safe_title = safe_title[:40]

                    file_name = f"{safe_title}.html"
                    file_name = re.sub(r'[\\/:*?"<>|\r\n]+', '_', file_name)

                    # 按业务分类创建子目录（优先使用 breadcrumb 第一级）
                    nav_dir = ""
                    if breadcrumb and len(breadcrumb) > 0:
                        nav_dir = re.sub(r'[\\/:*?"<>|\r\n]+', '_', breadcrumb[0].strip())[:50]
                    if not nav_dir:
                        nav_dir = "其他"

                    target_dir = os.path.join(output_root, nav_dir)
                    os.makedirs(target_dir, exist_ok=True)

                    save_path = os.path.join(target_dir, file_name)
                    # 处理重名
                    counter = 1
                    while os.path.exists(save_path):
                        dir_part, fname = os.path.split(save_path)
                        name_no_ext, ext = os.path.splitext(fname)
                        save_path = os.path.join(dir_part, f"{name_no_ext}_{counter}{ext}")
                        counter += 1

                    with open(save_path, "w", encoding="utf-8") as f:
                        f.write(rendered_html)
                    agent_logger.info(f"[Tool] save_data | HTML 文件已保存: {save_path}")
                except Exception as html_err:
                    agent_logger.warning(f"[Tool] save_data | 保存 HTML 文件失败 | url={url[:60]} | {html_err}")
            except Exception as write_err:
                agent_logger.warning(f"[Tool] save_data 写入失败 | url={url[:60]} | {write_err}")

        agent_logger.info(f"[Tool] save_data | 保存完成 | saved={saved} | hitl_required={hitl_required}")
        return json.dumps({
            "saved_count": saved,
            "csv_path": csv_writer.filepath,
            "output_dir": output_root,
            "hitl_required": hitl_required
        }, ensure_ascii=False)

    except Exception as e:
        agent_logger.error(f"[Tool] save_data 失败 | {type(e).__name__}: {str(e)[:200]}")
        return json.dumps({"saved_count": 0, "csv_path": "", "output_dir": "", "hitl_required": False,
                          "error": str(e)[:200]}, ensure_ascii=False)


@tool
def finish_task(summary: str) -> str:
    """
    结束当前爬取任务，输出任务汇总。
    Agent 应在完成所有页面抓取或达到目标后调用此工具。

    参数:
      summary: 任务汇总描述（如：已抓取页面数、成功/失败统计等）

    返回:
      确认 JSON: {"status": "finished", "summary": str}
    """
    agent_logger.info(f"[Tool] finish_task | summary={summary[:200]}")
    return json.dumps({
        "status": "finished",
        "summary": summary
    }, ensure_ascii=False)