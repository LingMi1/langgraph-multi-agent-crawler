# -*- coding: utf-8 -*-
"""
智能定向爬取管线 — 导航发现模块

两跳导航发现：
  第一跳：首页 → 提取提示词 → gsmc + 一级栏目 + list/pages 分类
  第二跳：每个一级栏目页 → 再跑一次提取提示词 → 捕获内页才出现的下拉子栏目
  合并去重（同 URL 保留最深 ywlx 路径）→ 完整 1-4 级 URL 树

首页本身不输出内容（只用于导航发现）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from agents.models import SiteProfile
from agents.llm_pipeline import (
    get_prompt,
    chat_json,
    compress_html,
    _merge_nav_records,
    _base_host,
    _safe_filename,
)
from agents.fetcher import HttpxPlaywrightFetcher
from schemas import agent_logger

# ============================================================================
# 抓取
# ============================================================================

_fetcher: Optional[HttpxPlaywrightFetcher] = None


def _get_fetcher(crawler_config: dict = None) -> HttpxPlaywrightFetcher:
    """复用 graph.nodes 的全局 fetcher（带防盗链/重试/Playwright 降级）"""
    global _fetcher
    if _fetcher is None:
        from graph import nodes as gn
        _fetcher = gn._get_fetcher(crawler_config)
    return _fetcher


async def _fetch_page(url: str, crawler_config: dict = None) -> Optional[str]:
    """抓取页面 HTML；失败返回 None。检测到骨架（JS 渲染前空壳）时强制 Playwright 重抓。
    若页面是 404（常见于 LLM 把 .htm 链接误拼为 /目录/ 形式），自动尝试 .htm 变体。"""
    html, _ = await _fetch_page_with_url(url, crawler_config)
    return html


async def _fetch_page_with_url(url: str, crawler_config: dict = None):
    """抓取页面并返回 (html, 实际使用的 URL)。404 时尝试 .htm 变体，成功则返回变体 URL。"""
    fetcher = _get_fetcher(crawler_config)

    async def _do_fetch(u: str, force_render: bool = False) -> str:
        profile = SiteProfile(url=u, needs_js_render=force_render)
        page = await fetcher.fetch(u, profile)
        h = page.html or page.raw_html or ""
        if force_render or (not _is_skeleton_html(h)):
            return h
        agent_logger.info(f"[NavDiscovery] 缓存返回骨架 HTML，强制 Playwright 重抓 | {u[:60]}")
        page = await fetcher._fetch_playwright(u)
        return page.html or page.raw_html or ""

    try:
        html = await _do_fetch(url)
        if _is_404_page(html):
            # LLM 可能把 .htm 拼成 /目录/ 或去掉扩展名 → 尝试 .htm 变体
            alt = _to_htm_variant(url)
            if alt and alt != url:
                agent_logger.info(f"[NavDiscovery] 404 → 尝试 .htm 变体: {alt[:70]}")
                html2 = await _do_fetch(alt)
                if not _is_404_page(html2):
                    return html2, alt
                # 变体也 404：仍返回原结果，让上层按 404 丢弃
        return html, url
    except Exception as e:
        agent_logger.warning(f"[NavDiscovery] 抓取失败 {url[:80]}: {e}")
        return None, url


def _is_404_page(html: Optional[str]) -> bool:
    """判断 HTML 是否为 404 错误页。
    优先看 <title>（harbin 等站 404 页可能 >30KB，不能按长度短路）；
    兜底看头部关键词。"""
    if not html:
        return False
    # 1) <title> 含 404 / 未找到 / not found
    try:
        soup = BeautifulSoup(html[:30000], "html.parser")
        t = (soup.title.get_text(strip=True) if soup.title else "") or ""
        if any(k in t.lower() for k in ("404", "未找到", "not found", "错误提示", "页面不存在")):
            return True
    except Exception:
        pass
    # 2) 头部关键词兜底（仅当页面较短，避免正常长页误判）
    if len(html) <= 30000:
        head = html[:2000].lower()
        if ("404" in head or "未找到" in head or "not found" in head
                or "页面不存在" in head or "您访问的页面" in head):
            return True
    return False


def _to_htm_variant(url: str) -> str:
    """/a/b/ → /a/b.htm；/a/b → /a/b.htm（保留 query）"""
    try:
        u = urlparse(url)
        path = u.path.rstrip("/")
        if not path:
            return ""
        if path.endswith((".htm", ".html", ".php", ".jsp")):
            return ""
        return f"{u.scheme}://{u.netloc}{path}.htm" + (f"?{u.query}" if u.query else "")
    except Exception:
        return ""


def _is_skeleton_html(html: str) -> bool:
    """判断是否为 JS 渲染前骨架：链接极少且正文极短"""
    if not html or len(html) < 500:
        return False
    try:
        soup = BeautifulSoup(html, "html.parser")
        link_count = len(soup.find_all("a", href=True))
        body = soup.find("body")
        body_text_len = len(body.get_text(" ", strip=True)) if body else 0
        return link_count < 10 and body_text_len < 400
    except Exception:
        return False


# ============================================================================
# 导航发现
# ============================================================================

def _normalize_url(href: str, base: str) -> str:
    """相对路径 → 绝对 URL，过滤非法 scheme，去掉锚点 fragment（#xxx 不构成独立页面）"""
    if not href:
        return ""
    u = urljoin(base, href.strip())
    if u.startswith(("mailto:", "javascript:", "tel:", "data:")):
        return ""
    # 只保留 http/https
    if not u.startswith(("http://", "https://")):
        return ""
    # 去掉锚点：#sjxt 等同页定位，避免 hdwh.htm 与 hdwh.htm#sjxt 被判成两个栏目
    if "#" in u:
        u = u.split("#", 1)[0]
    return u


async def _extract_nav_from_page(
    url: str,
    home_url: str,
    crawler_config: dict = None,
) -> Dict:
    """单页导航提取：跑一次提取提示词"""
    html = await _fetch_page(url, crawler_config)
    if not html:
        return {"gsmc": "", "list_pages": [], "pages": []}

    compressed = compress_html(html)
    prompt = get_prompt("提取提示词.txt")
    url_note = (
        "\n\n【URL 输出规则（必须严格遵守）】URL 必须逐字复制 HTML 中 <a href> 的原值，"
        "保留扩展名（.htm/.html/.php/.aspx 等）和 query 参数，绝对禁止："
        "去掉 .htm/.html 扩展名、把 /a/b.htm 改写成 /a/b/ 或 /a/b、增加或删除末尾斜杠、"
        "改写路径大小写。例如 href=\"gywm.htm\" 输出 \"http://当前域名/gywm.htm\"，不能输出 \".../gywm/\"。"
    )
    user_content = (
        f"首页 URL: {home_url}\n"
        f"当前页面 URL: {url}\n"
        f"当前页面 HTML（压缩版）:\n{compressed}"
        f"{url_note}"
    )
    result = await chat_json(prompt, user_content)
    if not isinstance(result, dict):
        agent_logger.warning(f"[NavDiscovery] LLM 导航提取无有效输出 | {url[:80]}")
        return {"gsmc": "", "list_pages": [], "pages": []}
    n_list = len(result.get("list_pages") or [])
    n_pages = len(result.get("pages") or [])
    agent_logger.info(f"[NavDiscovery] LLM 原始返回 keys={list(result.keys())} "
                      f"list_pages={n_list} pages={n_pages} | {url[:60]}")
    if n_list + n_pages == 0 and result.get("gsmc"):
        agent_logger.warning(f"[NavDiscovery] LLM 返回空栏目，原始 JSON 前 300 字: "
                             f"{json.dumps(result, ensure_ascii=False)[:300]}")

    # 规范化 URL 为绝对地址
    for bucket in ("list_pages", "pages"):
        for rec in result.get(bucket) or []:
            if isinstance(rec, dict) and rec.get("url"):
                rec["url"] = _normalize_url(rec["url"], url)
    return result


async def discover_nav_tree(
    home_url: str,
    crawler_config: dict = None,
    max_level1: int = 20,
    max_second_hop: int = 12,
) -> Dict:
    """
    两跳导航发现，产出完整 1-4 级 URL 树。

    Returns:
        {
          "gsmc": str,
          "home_url": str,
          "records": [ {url, ywlx1, ywlx2, ywlx3, ywlx4, page_type, is_image_only}, ... ],
          "list_pages": [...],   # page_type=list 的记录
          "pages": [...]         # page_type=page 的记录
        }
    """
    agent_logger.info(f"[NavDiscovery] 第一跳：首页 {home_url[:60]}")
    home_result = await _extract_nav_from_page(home_url, home_url, crawler_config)
    gsmc = home_result.get("gsmc", "")

    # 一级栏目（list+pages）
    level1 = [
        rec for rec in (home_result.get("list_pages") or []) + (home_result.get("pages") or [])
        if isinstance(rec, dict) and rec.get("url")
    ]
    agent_logger.info(f"[NavDiscovery] 首页发现 {len(level1)} 个一级栏目，gsmc={gsmc}")

    all_records: List[Dict] = list(level1)
    second_hop_urls = [r["url"] for r in level1][:max_second_hop]

    # 第二跳：每个一级栏目页再跑一次（捕获内页下拉子栏目）
    sem = asyncio.Semaphore(3)
    async def _second_hop(u: str):
        async with sem:
            agent_logger.info(f"[NavDiscovery] 第二跳：{u[:70]}")
            r = await _extract_nav_from_page(u, home_url, crawler_config)
            sub = [
                rec for rec in (r.get("list_pages") or []) + (r.get("pages") or [])
                if isinstance(rec, dict) and rec.get("url")
            ]
            # 用子栏目记录的 ywlx 覆盖一级的（更深路径）
            return sub

    hop2_results = await asyncio.gather(*[_second_hop(u) for u in second_hop_urls], return_exceptions=True)
    for sub in hop2_results:
        if isinstance(sub, list):
            all_records.extend(sub)
        else:
            agent_logger.warning(f"[NavDiscovery] 第二跳异常: {sub}")

    # 合并去重（同 URL 保留最深 ywlx）
    merged = _merge_nav_records(all_records)
    merged.sort(key=lambda r: (r.get("ywlx1") or "", r.get("ywlx2") or "", r.get("ywlx3") or "", r.get("ywlx4") or ""))

    list_pages = [r for r in merged if r.get("page_type") == "list"]
    pages = [r for r in merged if r.get("page_type") != "list"]

    agent_logger.info(
        f"[NavDiscovery] 导航树完成: 共 {len(merged)} 条 "
        f"(列表页 {len(list_pages)} / 单页 {len(pages)})"
    )
    return {
        "gsmc": gsmc,
        "home_url": home_url,
        "records": merged,
        "list_pages": list_pages,
        "pages": pages,
    }
