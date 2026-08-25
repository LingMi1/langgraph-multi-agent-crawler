# -*- coding: utf-8 -*-
"""
智能定向爬取管线 — 主抓取模块

按导航树（1-4级）抓取栏目页 / 列表页详情 / 图集，两段式 LLM 清洗后输出：
  - output/<域名>/ywlx1/ywlx2/ywlx3/.../title.html  卡片壳 HTML
  - output/<域名>/ 同级 CSV（全字段）

两段式清洗：LLM 返回正文容器定位签名 + title + 元信息 → 代码搬 outerHTML
（原 class/style/CSS 零损耗，长文不截断）→ 节点内噪音用 _strip_nav_noise 二次清。
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup

import config
from schemas import agent_logger

from agents.llm_pipeline import (
    get_prompt,
    chat_json,
    compress_html,
    _safe_filename,
    _base_host,
)
from agents.nav_discovery import discover_nav_tree, _get_fetcher, _normalize_url, _fetch_page, _fetch_page_with_url, _is_404_page

# ============================================================================
# 复用 graph.nodes 的清洗 / 图片嵌入 / LLM
# ============================================================================

def _gn():
    from graph import nodes as gn
    return gn


def _strip_nav_noise(html: str) -> str:
    return _gn()._strip_nav_noise(html)


async def _embed_images(html: str, page_url: str, user_agent: str, extra_headers: dict, cookies: dict = None):
    """复用 graph.nodes._embed_images_in_html（失败图整块删除，不占位）"""
    gn = _gn()
    html_new, processed, failed = await gn._embed_images_in_html(
        html, page_url, page_url, user_agent, extra_headers, cookies or {},
        None,
    )
    return html_new, processed, failed


# ============================================================================
# 两段式清洗
# ============================================================================

def _resolve_css_selector(soup: BeautifulSoup, selector: str) -> Optional[Any]:
    """按 LLM 返回的定位签名找正文容器。支持 id/class/tag/组合。"""
    if not selector:
        return None
    try:
        return soup.select_one(selector)
    except Exception:
        return None


def _extract_title_rule(html: str, llm_meta: Dict) -> str:
    """标题：优先用 LLM 返回的 title；为空则用页面 <title>"""
    t = (llm_meta.get("title") or "").strip()
    if t:
        return t
    try:
        soup = BeautifulSoup(html, "html.parser")
        return (soup.title.string or "").strip() if soup.title else ""
    except Exception:
        return ""


def _extract_meta_fields(soup: BeautifulSoup, llm_meta: Dict, page_url: str) -> Dict:
    """元信息：riqi/source/author/views，LLM 没有就从页面抄"""
    meta = {
        "riqi": (llm_meta.get("riqi") or llm_meta.get("riji") or "").strip(),
        "source": (llm_meta.get("source") or "").strip(),
        "author": (llm_meta.get("author") or "").strip(),
        "views": (llm_meta.get("views") or "").strip(),
        "url": page_url,
    }
    # 从正文找日期（若 LLM 没给）
    if not meta["riqi"]:
        for txt in soup.find_all(string=re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}")):
            s = txt.strip()
            m = re.search(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)", s)
            if m:
                meta["riqi"] = m.group(1).replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
                break
    return meta


async def clean_content_page(
    html: str, page_url: str, home_url: str, gsmc: str,
    user_agent: str, extra_headers: dict, cookies: dict = None,
    idx: str = "", ywlx: Tuple[str, str, str, str] = ("", "", "", ""),
) -> Optional[Dict]:
    """
    两段式清洗一个内容页。

    Returns dict:
      {title, content_html, riqi, source, author, views, url}
    content_html = 从原站 DOM 搬 outerHTML 的正文容器（原 class/style 保留）
    """
    ywlx1, ywlx2, ywlx3, ywlx4 = (ywlx + ("", "", "", ""))[:4]
    prompt = get_prompt("清洗提示词.txt")
    compressed = compress_html(html)
    # 两段式说明：LLM 只返回正文容器定位 + 元信息，content_html 由代码从原 DOM 搬 outerHTML 生成
    two_stage_note = (
        "\n\n【两段式执行说明（必须遵守）】本任务采用两段式清洗："
        "你只需输出正文容器定位选择器 content_selector（CSS 选择器，如 '#content' 或 '.article-body'）"
        "以及 title / riqi / source / author / views；content_html 字段输出空字符串，"
        "正文 HTML 由系统按 content_selector 从原站 DOM 原样提取（保留 class/style）。"
        "extract_status 照常输出 success/failed。其余业务字段 gsmc/index/ywlx1-4/zdr/timestamp/download_link 原样继承输入值。"
    )
    user_content = (
        f"gsmc: {gsmc}\n首页 URL: {home_url}\n当前页面 URL: {page_url}\n"
        f"index: {idx}\nywlx1: {ywlx1}\nywlx2: {ywlx2}\nywlx3: {ywlx3}\nywlx4: {ywlx4}\n"
        f"当前页面 HTML（压缩版）:\n{compressed}"
        f"{two_stage_note}"
    )
    meta = await chat_json(prompt, user_content)
    if not isinstance(meta, dict):
        agent_logger.warning(f"[Clean] LLM 清洗无有效输出，跳过 | {page_url[:60]}")
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    # 正文容器
    content_el = None
    selector = (meta.get("content_selector") or meta.get("selector") or "").strip()
    if selector:
        content_el = _resolve_css_selector(soup, selector)
    if content_el is None:
        # 兜底：用正文区域常见 id/class
        for sel in ("article", ".article", ".content", ".detail", ".news-content", "#content"):
            content_el = _resolve_css_selector(soup, sel)
            if content_el:
                break

    if content_el is None:
        agent_logger.warning(f"[Clean] 未定位到正文容器 | {page_url[:60]}")
        return None

    content_html = str(content_el)
    # 节点内噪音
    content_html = _strip_nav_noise(content_html)

    # 二次清洗：删除正文容器内残余面包屑/分享/相关推荐
    content_html = _strip_inner_noise(content_html)

    title = _extract_title_rule(html, meta)
    meta_fields = _extract_meta_fields(soup, meta, page_url)

    return {
        "title": title,
        "content_html": content_html,
        "url": page_url,
        **meta_fields,
    }


def _strip_inner_noise(content_html: str) -> str:
    """正文容器内的轻量噪音清除：分享/点赞/相关推荐/二维码"""
    try:
        soup = BeautifulSoup(content_html, "html.parser")
    except Exception:
        return content_html
    _noise = re.compile(
        r"(分享到|分享至|微信分享|朋友圈|点赞|点个赞|相关阅读|相关推荐|推荐阅读|"
        r"上一篇|下一篇|上一条|下一条|返回列表|返回上级|扫一扫|二维码|"
        r"标签[:：]|关键词[:：])",
        re.I,
    )
    for el in soup.find_all(["div", "p", "span", "ul", "section"]):
        if _noise.search(el.get_text(" ", strip=True)) and len(el.get_text(" ", strip=True)) < 60:
            # 短噪音块整块删除
            el.decompose()
    return str(soup)


# ============================================================================
# 列表页 → 详情链接
# ============================================================================

def _is_allowed_detail_url(u: str, home_url: str) -> bool:
    """详情链接域名白名单：只允许站内 + 微信公众号（列表页常混入外链如 gov.cn / 媒体转载）"""
    host = (urlparse(u).netloc or "").lower()
    home_host = (urlparse(home_url).netloc or "").lower()
    if host == home_host:
        return True
    # 公众号正文属于官网的延伸内容，允许
    if host in ("mp.weixin.qq.com", "weixin.qq.com"):
        return True
    return False


async def extract_detail_links(
    html: str, page_url: str, home_url: str, gsmc: str,
    is_image_only: bool = False,
) -> Tuple[List[str], bool]:
    """列表页 → (详情链接列表, is_image_only)。新闻列表页提示词自带图集判定。"""
    prompt = get_prompt("新闻列表页提示词.txt")
    compressed = compress_html(html)
    user_content = (
        f"gsmc: {gsmc}\n首页 URL: {home_url}\n当前列表页 URL: {page_url}\n"
        f"当前页面 HTML（压缩版）:\n{compressed}"
    )
    result = await chat_json(prompt, user_content)
    if not isinstance(result, dict):
        return [], False
    is_image_only = bool(result.get("is_image_only"))
    links = []
    for item in result.get("detail_links") or []:
        if isinstance(item, dict) and item.get("url"):
            u = _normalize_url(item["url"], page_url)
            if u and _is_allowed_detail_url(u, home_url):
                links.append(u)
    return links, is_image_only


async def collect_gallery_images(
    html: str, page_url: str, home_url: str, gsmc: str,
    ywlx: Tuple[str, str, str, str] = ("", "", "", ""),
) -> Tuple[List[str], str]:
    """图集页收图 → (图片 URL 列表, LLM 拼好的 content_html)。"""
    prompt = get_prompt("图片列表页提示词.txt")
    compressed = compress_html(html)
    ywlx1, ywlx2, ywlx3, ywlx4 = (ywlx + ("", "", "", ""))[:4]
    user_content = (
        f"gsmc: {gsmc}\n首页 URL: {home_url}\n当前图集页 URL: {page_url}\n"
        f"ywlx1: {ywlx1}\nywlx2: {ywlx2}\nywlx3: {ywlx3}\nywlx4: {ywlx4}\n"
        f"当前页面 HTML（压缩版）:\n{compressed}"
    )
    result = await chat_json(prompt, user_content)
    if not isinstance(result, dict):
        return [], ""
    imgs = []
    for item in result.get("image_list") or []:
        if isinstance(item, dict) and item.get("img_url"):
            u = _normalize_url(item["img_url"], page_url)
            if u:
                imgs.append(u)
    content_html = result.get("content_html") or ""
    # LLM 没拼 content_html 时兜底用 image_list 拼
    if not content_html and imgs:
        content_html = "".join(f'<p><img src="{u}" alt=""></p>' for u in imgs)
    return imgs, content_html


# ============================================================================
# 输出：卡片壳 HTML + CSV
# ============================================================================

def _render_card_html(
    page: Dict,
    gsmc: str,
    ywlx: Tuple[str, str, str, str],
) -> str:
    """
    按 正文渲染代码.txt 契约渲染卡片壳：
      头部 title / riqi / source + 正文 content_html + 原站 CSS
    """
    title = page.get("title") or ""
    riqi = page.get("riqi") or page.get("riji") or ""
    source = page.get("source") or ""
    author = page.get("author") or ""
    content_html = page.get("content_html") or ""

    # 卡片壳：简洁内联样式，标题+元信息头 + 正文
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; line-height: 1.8; color: #333; background: #f5f6f7; margin: 0; padding: 20px; }}
  .card {{ max-width: 860px; margin: 0 auto; background: #fff; border-radius: 8px; box-shadow: 0 2px 12px rgba(0,0,0,.08); padding: 28px 32px; }}
  .card h1 {{ font-size: 24px; color: #222; margin: 0 0 16px; line-height: 1.4; }}
  .card .meta {{ font-size: 13px; color: #999; border-bottom: 1px solid #eee; padding-bottom: 14px; margin-bottom: 20px; }}
  .card .meta span {{ margin-right: 18px; }}
  .card .content {{ font-size: 16px; }}
  .card .content img {{ max-width: 100%; height: auto; }}
  .card .content table {{ border-collapse: collapse; }}
  .card .content table td, .card .content table th {{ border: 1px solid #ddd; padding: 6px 10px; }}
  .card .content p {{ margin: 12px 0; }}
</style>
</head>
<body>
<div class="card">
  <h1>{title}</h1>
  <div class="meta">
    <span>来源：{source or "—"}</span>
    <span>作者：{author or "—"}</span>
    <span>日期：{riqi or "—"}</span>
  </div>
  <div class="content">
    {content_html}
  </div>
</div>
</body>
</html>"""
    return html_doc


def _ywlx_dirs(ywlx: Tuple[str, str, str, str], out_root: Path) -> Path:
    """ywlx1/2/3 → 目录（空级跳过）"""
    p = out_root
    for seg in ywlx:
        if seg:
            p = p / _safe_filename(seg, 60)
    return p


def _csv_path(out_root: Path, gsmc: str) -> Path:
    return out_root / f"{_safe_filename(gsmc, 40)}.csv"


CSV_FIELDS = [
    "gsmc", "index", "ywlx1", "ywlx2", "ywlx3", "ywlx4",
    "url", "title", "riqi", "source", "author", "views",
    "zdr", "timestamp", "download_link",
]


def _save_csv(out_root: Path, gsmc: str, rows: List[Dict]):
    path = _csv_path(out_root, gsmc)
    file_exists = path.exists()
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in CSV_FIELDS})


# ============================================================================
# 主流程
# ============================================================================

async def run_smart_crawl(
    home_url: str,
    output_dir: str = None,
    crawler_config: dict = None,
    log_callback=None,
    max_detail_per_list: int = 50,
    only_nav_tree: bool = False,
    max_second_hop: int = 12,
    max_records: int = 0,  # 调试用：只处理前 N 个栏目（0=全部）
) -> Dict:
    """
    智能定向爬取主入口。

    Args:
        home_url: 站点首页
        output_dir: 输出根目录（默认 config.LOCAL_BACKUP_DIR）
        only_nav_tree: True 时只跑导航发现（调试用）
    """
    if output_dir is None:
        output_dir = config.LOCAL_BACKUP_DIR
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    def _log(msg: str):
        agent_logger.info(f"[SmartCrawl] {msg}")
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    # ── ① 导航发现 ──
    _log(f"导航发现：{home_url[:60]}")
    tree = await discover_nav_tree(home_url, crawler_config, max_second_hop=max_second_hop)
    gsmc = tree["gsmc"] or _base_host(home_url)
    records = tree["records"]
    _log(f"导航树：{len(records)} 条栏目（列表 {len(tree['list_pages'])} / 单页 {len(tree['pages'])}）")
    if only_nav_tree:
        return {"tree": tree, "rows": [], "summary": {"gsmc": gsmc, "nav_records": len(records)}}

    rows: List[Dict] = []
    page_idx = 1
    emitted_detail_urls: set = set()  # 跨栏目详情去重（如「信息公开」与「公示公告」指向同一批详情）

    user_agent = config.USER_AGENT
    extra_headers = {"Referer": home_url}

    # ── ② 抓取 + 清洗 ──
    processed_count = 0
    for rec in records:
        if max_records and processed_count >= max_records:
            break
        processed_count += 1
        url = rec.get("url", "")
        if not url:
            continue
        page_type = rec.get("page_type")
        nav_image_only = bool(rec.get("is_image_only"))
        ywlx = (rec.get("ywlx1") or "", rec.get("ywlx2") or "", rec.get("ywlx3") or "", rec.get("ywlx4") or "")
        idx = rec.get("index") or str(page_idx)

        _log(f"处理栏目 [{page_type}] {ywlx[0]}/{ywlx[1]}/{ywlx[2]}/{ywlx[3]} | {url[:60]}")
        html, actual_url = await _fetch_page_with_url(url, crawler_config)
        if not html or _is_404_page(html):
            agent_logger.warning(f"[SmartCrawl] 抓取失败或 404，跳过 | {url[:60]}")
            continue
        # 实际 URL（可能已修正为 .htm 变体），相对链接解析以此为准
        page_base = actual_url or url

        # 单页 → 直接清洗输出
        if page_type != "list":
            cleaned = await clean_content_page(
                html, page_base, home_url, gsmc, user_agent, extra_headers, None, idx=idx, ywlx=ywlx,
            )
            if cleaned and cleaned.get("content_html"):
                await _emit_page(cleaned, gsmc, ywlx, out_root, rows, page_idx, user_agent, extra_headers, None)
                page_idx += 1
            continue

        # 列表页 → 提取详情链接（提示词自带图集判定）
        detail_links, is_image_only = await extract_detail_links(html, page_base, home_url, gsmc, nav_image_only)
        _log(f"  列表页：详情 {len(detail_links)} 条, is_image_only={is_image_only}")

        # 图集页 → 直接收图
        if is_image_only:
            imgs, gallery_html = await collect_gallery_images(html, page_base, home_url, gsmc, ywlx)
            _log(f"  图集收图 {len(imgs)} 张")
            if gallery_html:
                cleaned = {
                    "title": f"{ywlx[1] or ywlx[0] or ywlx[2]}图集",
                    "content_html": gallery_html,
                    "url": page_base, "riqi": "", "source": "", "author": "", "views": "",
                }
                await _emit_page(cleaned, gsmc, ywlx, out_root, rows, page_idx, user_agent, extra_headers, None)
                page_idx += 1
            continue

        # 普通列表页 → 抓详情页
        for durl in detail_links[:max_detail_per_list]:
            # 跨栏目详情去重：同一 URL 已在前面栏目输出过则跳过
            if durl in emitted_detail_urls:
                continue
            emitted_detail_urls.add(durl)
            _log(f"  详情：{durl[:70]}")
            dhtml, dactual = await _fetch_page_with_url(durl, crawler_config)
            if not dhtml or _is_404_page(dhtml):
                continue
            dbase = dactual or durl
            cleaned = await clean_content_page(
                dhtml, dbase, home_url, gsmc, user_agent, extra_headers, None, idx=f"{idx}-{dbase[:30]}", ywlx=ywlx,
            )
            if cleaned and cleaned.get("content_html"):
                cleaned["url"] = dbase
                await _emit_page(cleaned, gsmc, ywlx, out_root, rows, page_idx, user_agent, extra_headers, None)
                page_idx += 1

    _log(f"完成：输出 {len(rows)} 页")
    return {"tree": tree, "rows": rows, "summary": {"gsmc": gsmc, "pages": len(rows)}}


async def _emit_page(
    cleaned: Dict,
    gsmc: str,
    ywlx: Tuple[str, str, str, str],
    out_root: Path,
    rows: List[Dict],
    page_idx: int,
    user_agent: str,
    extra_headers: dict,
    cookies: dict,
):
    """清洗结果 → 图片内嵌（base64，失败图整块删除）→ 卡片壳 HTML 落盘 + CSV 行"""
    url = cleaned.get("url", "")
    content_html = cleaned["content_html"]
    try:
        content_html, _p, _f = await _embed_images(content_html, url, user_agent, extra_headers, cookies)
    except Exception as e:
        agent_logger.warning(f"[SmartCrawl] 图片内嵌失败: {e} | {url[:60]}")
    cleaned["content_html"] = content_html

    card_html = _render_card_html(cleaned, gsmc, ywlx)
    dir_path = _ywlx_dirs(ywlx, out_root)
    dir_path.mkdir(parents=True, exist_ok=True)
    fname = _safe_filename(cleaned.get("title") or f"page_{page_idx}", 60) + ".html"
    file_path = dir_path / fname
    file_path.write_text(card_html, encoding="utf-8")

    row = {
        "gsmc": gsmc,
        "index": str(page_idx),
        "ywlx1": ywlx[0], "ywlx2": ywlx[1], "ywlx3": ywlx[2], "ywlx4": ywlx[3],
        "url": cleaned.get("url", ""),
        "title": cleaned.get("title", ""),
        "riqi": cleaned.get("riqi", ""),
        "source": cleaned.get("source", ""),
        "author": cleaned.get("author", ""),
        "views": cleaned.get("views", ""),
        "zdr": "", "timestamp": "", "download_link": "",
    }
    rows.append(row)
    _save_csv(out_root, gsmc, [row])
    agent_logger.info(f"[SmartCrawl] 输出 [{page_idx}] {fname}")


# 方便 CLI 调用
async def main_async(home_url: str, output_dir: str = None, **kw):
    return await run_smart_crawl(home_url, output_dir, **kw)
