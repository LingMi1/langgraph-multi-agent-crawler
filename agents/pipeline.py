"""
主流水线 — 多 Agent 异步协作编排器

将 Scout → Nav → Fetcher → Extractor → Storage 串联为完整的爬取链路。

流程:
  1. ScoutAgent.analyze(url)             → SiteProfile
  2. FetcherRouter.fetch(homepage_url)   → PageData (首页 HTML)
  3. NavAgent.extract_links(homepage)    → List[NavLink] (详情页链接)
  4. 对每个详情页链接 (并发):
     a. FetcherRouter.fetch(nav_link.url) → PageData
     b. ExtractorAgent.extract(page)      → PageData (cleaned)
     c. StorageManager.save(page)         → CrawlResult
"""

from __future__ import annotations

import asyncio
import os
from collections import deque
from typing import List, Optional, Callable, Set, Tuple
from urllib.parse import urlparse

from .models import SiteProfile, PageData, CrawlResult, NavLink
from .scout import PageScout
from .nav import NavigationParser
from .fetcher import HttpxPlaywrightFetcher
from .extractor import TrafilaturaExtractor, _is_list_page
from .storage import FileSystemStorage

from schemas import agent_logger
import config


class CrawlerPipeline:
    """
    多 Agent 爬取流水线 — 异步事件驱动架构。

    Usage:
        pipeline = CrawlerPipeline()
        stats = await pipeline.run("https://example.com")
    """

    def __init__(
        self,
        concurrency: int = 5,
        log_callback: Optional[Callable[[str], None]] = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        self._scout = PageScout()
        self._nav = NavigationParser()
        self._fetcher = HttpxPlaywrightFetcher()
        self._extractor = TrafilaturaExtractor()
        self._storage = FileSystemStorage()
        self._concurrency = concurrency
        self._log = log_callback or (lambda msg: print(msg))
        self._stop = stop_event or asyncio.Event()

        # 统计
        self._stats = {
            "total_detail_links": 0,
            "pages_fetched": 0,
            "pages_extracted": 0,
            "pages_saved": 0,
            "pages_skipped": 0,
            "pages_duplicate": 0,
            "pages_failed": 0,
        }

    # ==================================================================
    # 主入口
    # ==================================================================

    async def run(self, url: str) -> dict:
        """
        执行完整爬取流水线 — BFS 逐层遍历 (depth 1→4)。

        Returns:
            stats: dict 统计信息
        """
        self._log(f"启动多 Agent 爬虫流水线 | 目标: {url} | 并发: {self._concurrency}")

        # ── 输出目录 ──
        domain = urlparse(url).netloc.replace(":", "_")
        output_dir = os.path.join(config.LOCAL_BACKUP_DIR, domain)
        os.makedirs(output_dir, exist_ok=True)

        # ── 阶段 1: ScoutAgent ──
        if self._stop.is_set():
            return self._stats
        self._log("[阶段 1/5] ScoutAgent: 分析站点特征...")
        profile = await self._scout.analyze(url)
        self._storage.set_site_name(profile.title[:60])
        self._log(
            f"  站点画像 | 标题={profile.title[:40]} | JS渲染={profile.needs_js_render} "
            f"| 类型={profile.site_type} | 反爬={profile.anti_crawl_level}"
        )

        # ── 阶段 2: FetcherRouter (首页) ──
        if self._stop.is_set():
            return self._stats
        self._log("[阶段 2/5] FetcherRouter: 抓取首页...")
        homepage = await self._fetcher.fetch(url, profile)
        if not homepage.html:
            self._log(f"  ❌ 首页抓取失败 | html_len={len(homepage.html)}")
            self._log(f"  ⚠️  流水线终止")
            return self._stats
        self._log(f"  首页抓取成功 | 方法={homepage.fetch_method} | HTML长度={len(homepage.html)}")

        # ── 阶段 3: BFS 逐层链接发现 ──
        if self._stop.is_set():
            return self._stats
        self._log("[阶段 3/5] BFS 逐层链接发现 (最大深度=4)...")

        base_url = url  # 用于同域检查
        queue: deque[NavLink] = deque()
        seen_url_keys: Set[str] = set()
        detail_pages: List[Tuple[NavLink, PageData]] = []

        # 初始: 从首页导航提取 depth-1 链接
        hp_links = await self._nav.extract_links(homepage.html, profile, current_depth=0)
        for link in hp_links:
            key = self._url_key(link.url)
            if key not in seen_url_keys:
                seen_url_keys.add(key)
                queue.append(link)
        self._log(f"  首页导航: {len(hp_links)} 个一级链接入队")

        sem = asyncio.Semaphore(self._concurrency)
        bfs_round = 0

        while queue and not self._stop.is_set():
            bfs_round += 1
            # 取出当前批次 (上限 2×concurrency，防止一轮太多)
            batch: List[NavLink] = []
            while queue and len(batch) < self._concurrency * 4:
                batch.append(queue.popleft())

            self._log(f"  BFS 第 {bfs_round} 轮: 处理 {len(batch)} 个链接 (剩余队列: {len(queue)})")

            async def bfs_fetch_one(link: NavLink) -> None:
                async with sem:
                    try:
                        page = await self._fetcher.fetch(link.url, profile)
                        page.nav_path = link.nav_path
                        page.depth = link.depth
                        self._stats["pages_fetched"] += 1

                        if not page.html or len(page.html) < 100:
                            self._stats["pages_failed"] += 1
                            return

                        # 用 HTML 内容判断是否为列表页
                        loop = asyncio.get_running_loop()
                        text_check = await loop.run_in_executor(
                            None,
                            lambda: page.html and __import__("bs4").BeautifulSoup(
                                page.html, "html.parser"
                            ).find("body")
                        )
                        text_check = text_check.get_text(" ", strip=True) if text_check else ""
                        is_list, list_conf, reason = _is_list_page(page.html, text_check)

                        if is_list and link.depth < 4:
                            # 列表页 → 从 body 全量提取同域链接（仿 LangGraph BFS 模式）
                            body_links = await loop.run_in_executor(
                                None,
                                self._extract_body_links,
                                page.html, link.url, base_url
                            )
                            added = 0
                            new_depth = link.depth + 1
                            for abs_url, link_text in body_links:
                                key = self._url_key(abs_url)
                                if key not in seen_url_keys:
                                    seen_url_keys.add(key)
                                    # body 链接文本可能很长（文章标题），截断用于路径；清理 > 前缀
                                    clean_text = (link_text or "").lstrip("> \t\r\n")
                                    short_text = clean_text[:20] if clean_text else ""
                                    nl = NavLink(
                                        url=abs_url,
                                        text=short_text,
                                        nav_path=link.nav_path + [short_text] if short_text else link.nav_path,
                                        depth=new_depth,
                                        is_detail_page=True,
                                    )
                                    queue.append(nl)
                                    added += 1
                            if added:
                                self._log(
                                    f"    [列表页 depth={link.depth}] {link.text[:20]} → +{added} 子链接 (body全量) | "
                                    f"reason={reason}"
                                )

                        if not is_list:
                            # 详情页 → 加入待处理队列
                            detail_pages.append((link, page))

                    except Exception as e:
                        self._stats["pages_failed"] += 1
                        agent_logger.warning(f"[Pipeline BFS] 异常: {link.url[:60]} | {e}")

            await asyncio.gather(*[bfs_fetch_one(link) for link in batch])

        self._stats["total_detail_links"] = len(detail_pages)
        self._log(f"  BFS 完成 | 发现 {len(detail_pages)} 个详情页 | 队列耗尽={len(queue)==0}")

        if not detail_pages:
            self._log(f"  ⚠️  无详情页可抓取 | 输出目录: {output_dir}")
            return self._stats

        # ── 阶段 4: 并清洗 + 保存 ──
        if self._stop.is_set():
            return self._stats
        self._log(f"[阶段 4/5] 并发清洗+保存 {len(detail_pages)} 个页面 (并发={self._concurrency})...")

        async def process_detail(idx: int, link: NavLink, page: PageData) -> None:
            async with sem:
                try:
                    cleaned = await self._extractor.extract(page, profile)
                    self._stats["pages_extracted"] += 1

                    if cleaned.is_list_page_detected_at_extract or not cleaned.html:
                        self._stats["pages_skipped"] += 1
                        self._log(f"  [{idx+1}/{len(detail_pages)}] 跳过 | {link.url[:60]}")
                        return

                    result = await self._storage.save(cleaned, output_dir)
                    if result.status == "success":
                        self._stats["pages_saved"] += 1
                        path = "/".join(link.nav_path) if link.nav_path else "/"
                        self._log(
                            f"  [{idx+1}/{len(detail_pages)}] 已保存 | "
                            f"depth={link.depth} | {path} | {cleaned.title[:30]}"
                        )
                    elif result.status == "skipped_duplicate":
                        self._stats["pages_duplicate"] += 1
                        self._log(f"  [{idx+1}/{len(detail_pages)}] 重复跳过 | {link.url[:60]}")
                    elif result.status in ("skipped_empty", "skipped_list"):
                        self._stats["pages_skipped"] += 1
                    else:
                        self._stats["pages_failed"] += 1

                except Exception as e:
                    self._stats["pages_failed"] += 1
                    self._log(f"  [{idx+1}/{len(detail_pages)}] 异常: {str(e)[:80]}")

        tasks = [process_detail(i, link, page) for i, (link, page) in enumerate(detail_pages)]
        await asyncio.gather(*tasks)

        # ── 阶段 5: 汇总 ──
        self._log("=" * 50)
        self._log("流水线完成!")
        self._log(f"  详情页链接: {len(detail_pages)}")
        self._log(f"  成功抓取:   {self._stats['pages_fetched']}")
        self._log(f"  成功保存:   {self._stats['pages_saved']}")
        self._log(f"  跳过:       {self._stats['pages_skipped']}")
        self._log(f"  重复:       {self._stats['pages_duplicate']}")
        self._log(f"  失败:       {self._stats['pages_failed']}")
        self._log(f"  CSV 路径:   {await self._storage.get_csv_path()}")
        self._log("=" * 50)

        return self._stats

    @staticmethod
    def _url_key(url: str) -> str:
        """生成 URL 去重键（忽略协议/尾部斜杠/fragment/无意义 query）"""
        parsed = urlparse(url)
        key = f"{parsed.netloc}{parsed.path}"
        return key.rstrip("/").lower()

    @staticmethod
    def _extract_body_links(html: str, page_url: str, base_url: str) -> List[Tuple[str, str]]:
        """
        从页面 body 中提取所有同域内部链接（仿 nodes.py _extract_same_domain_links）。
        Returns: [(abs_url, link_text), ...]
        """
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin, urlparse

        soup = BeautifulSoup(html, "html.parser")
        body = soup.find("body") or soup
        links: List[Tuple[str, str]] = []
        seen: Set[str] = set()

        base_host = urlparse(base_url).netloc.lower()

        for a in body.find_all("a", href=True):
            href = a["href"].strip()
            if not href:
                continue
            # 跳过非页面协议
            low = href.lower()
            if any(low.startswith(s) for s in ("javascript:", "mailto:", "tel:", "#")):
                continue
            abs_url = urljoin(page_url, href)
            parsed = urlparse(abs_url)
            # 同域检查
            if parsed.netloc.lower() != base_host:
                continue
            # 跳过非 HTML 后缀
            path = parsed.path.lower()
            if path and any(
                path.endswith(ext)
                for ext in (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
                            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip",
                            ".rar", ".mp4", ".mp3", ".css", ".js", ".ico")
            ):
                continue
            key = f"{parsed.netloc}{path}".rstrip("/").lower()
            if key not in seen:
                seen.add(key)
                text = a.get_text(strip=True)[:50]
                links.append((abs_url, text))
        return links
