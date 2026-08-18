"""
agents/ — 多 Agent 智能路由架构

四个核心 Agent + 一个存储管理器：

  ScoutAgent     — 侦察兵:   analyze(url)           → SiteProfile
  NavAgent       — 领航员:   extract_links(html,...) → List[NavLink]
  FetcherRouter  — 下载路由: fetch(url, profile)     → PageData
  ExtractorAgent — 清洗专家: extract(page, profile)  → PageData (cleaned)
  StorageManager — 存储管理: save(page, dir)         → CrawlResult
"""

from .models import SiteProfile, NavLink, PageData, CrawlResult
from .interfaces import ScoutAgent, NavAgent, FetcherRouter, ExtractorAgent, StorageManager

# 具体实现类
from .scout import PageScout
from .nav import NavigationParser
from .fetcher import HttpxPlaywrightFetcher
from .extractor import TrafilaturaExtractor
from .storage import FileSystemStorage

__all__ = [
    # 数据模型
    "SiteProfile",
    "NavLink",
    "PageData",
    "CrawlResult",
    # 抽象接口
    "ScoutAgent",
    "NavAgent",
    "FetcherRouter",
    "ExtractorAgent",
    "StorageManager",
    # 具体实现
    "PageScout",
    "NavigationParser",
    "HttpxPlaywrightFetcher",
    "TrafilaturaExtractor",
    "FileSystemStorage",
]
