"""
核心组件: StorageManager — 存储管理器

职责: 接收清洗后的 HTML 和元数据，基于 nav_path 创建多级目录并保存。
     并发安全的 CSV 追加写入（文件锁保护）。

目录结构示例:
  output/example.com/
  ├── crawl_results.csv
  ├── 未分类/
  │   └── 某页面.html
  ├── 新闻中心/
  │   ├── 集团新闻/
  │   │   └── 某新闻.html
  │   └── 行业动态/
  │       └── 某动态.html
  └── 关于我们/
      └── 公司简介.html
"""

from __future__ import annotations

import os
import re
import csv
import asyncio
from typing import Dict, List, Set
from datetime import datetime
from urllib.parse import urlparse

# 跨平台文件锁
try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

from .models import PageData, CrawlResult
from .interfaces import StorageManager as StorageManagerInterface

from schemas import agent_logger


# ============================================================================
# CSV 字段定义（对齐目标模板）
# ============================================================================

CSV_FIELDS = [
    "sys_platfuuid", "brwidcl_cpmc", "ywlx",
    "ywlx1", "ywlx2", "ywlx3", "ywlx4",
    "tianextimejsj", "title", "html",
    "download_img_url", "img_title",
    "url",
]


# ============================================================================
# StorageManager 实现
# ============================================================================

class FileSystemStorage(StorageManagerInterface):
    """
    基于本地文件系统的存储管理器。

    特性:
      - 内容 MD5 去重 (seen_hashes)
      - 清洗后 HTML 内联写入 CSV
      - 并发安全 CSV 写入 (文件锁)
      - nav_path 分级目录 HTML 文件落盘
    """

    def __init__(self) -> None:
        self._base_dir: str = ""
        self._csv_path: str = ""
        self._csv_lock = asyncio.Lock()
        self._seen_hashes: Set[str] = set()
        self._site_name: str = ""  # bstudio_cgsmc
        self._stats: Dict[str, int] = {
            "total_saved": 0,
            "total_skipped": 0,
            "total_duplicate": 0,
            "total_failed": 0,
        }

    def set_site_name(self, name: str) -> None:
        """设置站点名称（用于 CSV 的 bstudio_cgsmc 字段）"""
        self._site_name = name

    # ==================================================================
    # save — 保存单个页面
    # ==================================================================

    async def save(self, page: PageData, base_output_dir: str) -> CrawlResult:
        """
        保存单个清洗后的页面。

        Args:
            page:            清洗后的 PageData
            base_output_dir: 输出根目录 (如 "output/example.com")

        Returns:
            CrawlResult 元数据
        """
        self._base_dir = base_output_dir
        self._csv_path = os.path.join(base_output_dir, "crawl_results.csv")
        os.makedirs(base_output_dir, exist_ok=True)

        # 提取 nav_path 各级
        ywlx = self._split_nav_path(page.nav_path)

        result = CrawlResult(
            brwidcl_cpmc=self._site_name,
            ywlx=ywlx["full"],
            ywlx1=ywlx["l1"], ywlx2=ywlx["l2"], ywlx3=ywlx["l3"], ywlx4=ywlx["l4"],
            url=page.url,
            tianextimejsj=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            title=page.title,
            html="",
            download_img_url=page.images_urls[0] if page.images_urls else "",
            img_title=page.images_alts[0] if page.images_alts else "",
            content_hash=page.content_hash,
        )

        # ── 空内容 ──
        if not page.html or len(page.html.strip()) < 50:
            result.status = "skipped_empty"
            result.error_message = "清洗后内容为空或过短"
            self._stats["total_skipped"] += 1
            await self._append_csv(result)
            return result

        # ── 列表页 ──
        if page.is_list_page_detected_at_extract:
            result.status = "skipped_list"
            result.error_message = "ExtractorAgent 检测为列表页"
            self._stats["total_skipped"] += 1
            await self._append_csv(result)
            return result

        # ── 内容去重 ──
        if page.content_hash and page.content_hash in self._seen_hashes:
            result.status = "skipped_duplicate"
            result.error_message = f"内容重复 (hash={page.content_hash[:8]})"
            self._stats["total_duplicate"] += 1
            agent_logger.info(
                f"[StorageManager] 跳过重复内容 | hash={page.content_hash[:8]} | {page.url[:60]}"
            )
            await self._append_csv(result)
            return result

        self._seen_hashes.add(page.content_hash)

        # ── 构建文件路径 ──
        file_path = self._build_file_path(page)
        full_path = os.path.join(base_output_dir, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        full_path = self._resolve_conflict(full_path)

        # ── 写入 HTML 文件 ──
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._write_html, full_path, page.html
            )
        except Exception as e:
            agent_logger.error(f"[StorageManager] 保存 HTML 失败: {e} | {page.url[:80]}")
            result.status = "failed"
            result.error_message = str(e)[:200]
            self._stats["total_failed"] += 1
            await self._append_csv(result)
            return result

        # ── 写入 CSV（含清洗后 HTML 内联） ──
        result.file_path = os.path.relpath(full_path, base_output_dir)
        result.html = page.html  # 清洗后正文 HTML，内联写入 CSV
        result.status = "success"
        self._stats["total_saved"] += 1

        agent_logger.info(
            f"[StorageManager] 已保存 | path={result.file_path} | "
            f"len={len(page.html)} | title={page.title[:30]}"
        )
        await self._append_csv(result)
        return result

    # ==================================================================
    # 查询接口
    # ==================================================================

    async def get_csv_path(self) -> str:
        return self._csv_path

    async def get_stats(self) -> dict:
        """返回存储统计（合并 BFS 收集的 detail_links 等外部指标）"""
        return dict(self._stats)

    # ==================================================================
    # 内部方法
    # ==================================================================

    @staticmethod
    def _split_nav_path(nav_path: List[str]) -> dict:
        """将 nav_path 列表拆分为 ywlx (完整路径) + ywlx1~ywlx4 四个字段"""
        padded = list(nav_path) + ["", "", "", ""]
        full = "/".join(filter(None, nav_path)) if nav_path else ""
        return {"full": full, "l1": padded[0], "l2": padded[1], "l3": padded[2], "l4": padded[3]}

    def _build_file_path(self, page: PageData) -> str:
        """
        根据 nav_path 构建文件存储路径。

        示例: nav_path=['新闻中心', '行业动态'] → "新闻中心/行业动态/页面标题.html"
              nav_path=[] → "未分类/页面标题.html"
        """
        # 子目录: nav_path 逐级拼接
        sub_dirs = [self._sanitize_dirname(d) for d in page.nav_path if d]

        # 文件名: 基于 title 或 URL 末段
        filename = self._generate_filename(page)

        if sub_dirs:
            return os.path.join(*sub_dirs, filename)
        else:
            return os.path.join("未分类", filename)

    def _generate_filename(self, page: PageData) -> str:
        """生成安全的文件名"""
        # 优先使用标题
        name = page.title.strip() if page.title else ""

        if not name:
            # 降级: 取 URL 路径末段
            parsed = urlparse(page.url)
            path = parsed.path.strip("/")
            if path:
                name = path.split("/")[-1]
                # 去掉扩展名
                name = re.sub(r'\.[^.]+$', '', name)
            else:
                name = "page"

        # 清洗文件名
        name = re.sub(r'[\\/:*?"<>|]', "_", name)
        name = name.strip("._ ")
        if len(name) > 60:
            name = name[:60]

        if not name:
            name = "page"

        return f"{name}.html"

    @staticmethod
    def _sanitize_dirname(name: str) -> str:
        """清洗目录名"""
        if not name:
            return "未分类"
        name = re.sub(r'[\\/:*?"<>|]', "_", name)
        name = name.strip("._ ")
        if len(name) > 40:
            name = name[:40]
        if not name:
            return "未分类"
        return name

    def _resolve_conflict(self, path: str) -> str:
        """处理重名文件，追加 _1, _2 后缀"""
        if not os.path.exists(path):
            return path

        dir_part, fname = os.path.split(path)
        name_no_ext, ext = os.path.splitext(fname)
        counter = 1
        while True:
            new_path = os.path.join(dir_part, f"{name_no_ext}_{counter}{ext}")
            if not os.path.exists(new_path):
                return new_path
            counter += 1

    @staticmethod
    def _write_html(path: str, html: str) -> None:
        """写入 HTML 文件（同步，在线程池中执行）"""
        with open(path, "w", encoding="utf-8") as f:
            # 写入完整 HTML 文档结构
            f.write("<!DOCTYPE html>\n<html>\n<head>\n")
            f.write('<meta charset="utf-8">\n')
            f.write('<meta name="viewport" content="width=device-width, initial-scale=1">\n')
            f.write("</head>\n<body>\n")
            f.write(html)
            f.write("\n</body>\n</html>")

    async def _append_csv(self, result: CrawlResult) -> None:
        """
        追加写入 CSV（文件锁保护并发安全）。
        """
        async with self._csv_lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._csv_write_sync, result
            )

    def _csv_write_sync(self, result: CrawlResult) -> None:
        """同步 CSV 写入（在 executor 中执行）"""
        file_exists = os.path.exists(self._csv_path)
        try:
            with open(self._csv_path, "a", newline="", encoding="utf-8-sig") as f:
                # 文件锁 (跨平台兼容)
                self._lock_file(f)

                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                if not file_exists:
                    writer.writeheader()

                writer.writerow({
                    "sys_platfuuid": result.sys_platfuuid,
                    "brwidcl_cpmc": result.brwidcl_cpmc,
                    "ywlx": result.ywlx,
                    "ywlx1": result.ywlx1,
                    "ywlx2": result.ywlx2,
                    "ywlx3": result.ywlx3,
                    "ywlx4": result.ywlx4,
                    "tianextimejsj": result.tianextimejsj,
                    "title": result.title,
                    "html": result.html,
                    "download_img_url": result.download_img_url,
                    "img_title": result.img_title,
                    "url": result.url,
                })
        except Exception as e:
            agent_logger.error(f"[StorageManager] CSV 写入失败: {e}")

    @staticmethod
    def _lock_file(f) -> None:
        """跨平台文件锁"""
        if _HAS_FCNTL:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass  # Windows 或不可用环境静默降级
