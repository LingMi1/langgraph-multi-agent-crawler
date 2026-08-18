"""
多 Agent 架构 — 抽象接口定义

每个 Agent 声明其输入/输出契约，通过 Protocol/ABC 保持类型安全。
所有 Agent 均为异步调用 (async def)，兼容将来的并发抓取。
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List, Optional, AsyncIterator

from .models import SiteProfile, NavLink, PageData, CrawlResult


# ============================================================================
# Agent 1: ScoutAgent — 侦察兵
# ============================================================================

class ScoutAgent(ABC):
    """
    职责：接收种子 URL，分析首页 HTML 特征。
    输入 → 输出：
      analyze(url: str) → SiteProfile
    """

    @abstractmethod
    async def analyze(self, url: str) -> SiteProfile:
        """
        分析目标站点首页，返回 SiteProfile。

        Args:
            url: 站点首页 URL

        Returns:
            SiteProfile 包含 JS 渲染需求、站点类型、反爬强度等信息
        """
        ...


# ============================================================================
# Agent 2: NavAgent — 领航员 & 过滤器
# ============================================================================

class NavAgent(ABC):
    """
    职责：解析页面导航结构，提取带层级路径的 URL，过滤列表页和首页链接。
    输入 → 输出：
      extract_links(html: str, profile: SiteProfile) → List[NavLink]
    """

    @abstractmethod
    async def extract_links(
        self, html: str, profile: SiteProfile, current_depth: int = 0
    ) -> List[NavLink]:
        """
        解析 HTML 的导航栏，提取所有详情页链接。

        Args:
            html:       页面 HTML (首页或子页面)
            profile:    站点画像 (ScoutAgent 输出)
            current_depth: 当前页面所在导航深度，用于计算子链接的相对深度

        Returns:
            NavLink 列表，仅包含 is_detail_page=True 且 is_homepage=False 的链接
            (is_list_detected=False 的链接已在方法内部过滤，不在返回列表中)
        """
        ...


# ============================================================================
# Agent 3: FetcherRouter — 下载路由
# ============================================================================

class FetcherRouter(ABC):
    """
    职责：根据 SiteProfile 决定下载策略，执行实际的页面抓取。
    输入 → 输出：
      fetch(url: str, profile: SiteProfile) → PageData
    """

    @abstractmethod
    async def fetch(self, url: str, profile: SiteProfile) -> PageData:
        """
        抓取单个页面。

        Args:
            url:     目标页面 URL
            profile: 站点画像

        Returns:
            PageData (html 字段为原始 HTML，title 可能为空待 ExtractorAgent 补充)
        """
        ...

    @abstractmethod
    async def fetch_batch(
        self, urls: List[str], profile: SiteProfile, concurrency: int = 5
    ) -> AsyncIterator[PageData]:
        """
        并发抓取多个页面，返回异步迭代器（边抓取边产出）。

        Args:
            urls:         目标 URL 列表
            profile:      站点画像
            concurrency:  并发数

        Yields:
            PageData 逐个产出，支持流式处理
        """
        ...


# ============================================================================
# Agent 4: ExtractorAgent — 清洗专家
# ============================================================================

class ExtractorAgent(ABC):
    """
    职责：清洗 HTML → 去头尾侧边栏 → 留正文+图片；列表页二次拦截。
    输入 → 输出：
      extract(page: PageData, profile: SiteProfile) → PageData
    """

    @abstractmethod
    async def extract(
        self, page: PageData, profile: SiteProfile
    ) -> PageData:
        """
        清洗页面内容。

        Args:
            page:    包含原始 html 的 PageData
            profile: 站点画像

        Returns:
            清洗后的 PageData:
            - html                   → 只含正文+图片的干净 HTML
            - title                  → 补充后的标题
            - content_quality_score   → 0-1 质量评分
            - is_list_page_detected_at_extract → 是否二次拦截
            - images_count           → 图片数量
        """
        ...


# ============================================================================
# 核心组件: StorageManager — 存储管理器
# ============================================================================

class StorageManager(ABC):
    """
    职责：根据 nav_path 创建多级文件夹，保存 HTML，追加写入 CSV。

    线程安全：所有写操作使用 asyncio.Lock 或文件锁保护。
    """

    @abstractmethod
    async def save(self, page: PageData, base_output_dir: str) -> CrawlResult:
        """
        保存单个清洗后的页面。

        Args:
            page:            清洗后的 PageData
            base_output_dir: 输出根目录 (如 "output/example.com")

        Returns:
            CrawlResult 包含 file_path、status 等 CSV 元数据
        """
        ...

    @abstractmethod
    def set_site_name(self, name: str) -> None:
        """设置站点名称（用于 CSV 的 bstudio_cgsmc 字段）"""
        ...

    @abstractmethod
    async def get_csv_path(self) -> str:
        """返回 crawl_results.csv 的绝对路径"""
        ...

    @abstractmethod
    async def get_stats(self) -> dict:
        """
        返回存储统计
        Returns:
            {"total_saved": int, "total_skipped": int, "total_failed": int, "disk_usage_mb": float}
        """
        ...
