"""
多 Agent 架构 — Pydantic 数据模型

- SiteProfile:      ScoutAgent 输出 (站点画像)
- NavLink:          NavAgent 输出 (导航链接 + 列表/详情判定)
- PageData:         FetcherRouter → ExtractorAgent → StorageManager 之间流转
- CrawlResult:      StorageManager 落盘后返回的元数据 (写入 CSV)
"""

from __future__ import annotations
import uuid
from typing import List, Dict, Any
from datetime import datetime

from pydantic import BaseModel, Field


# ============================================================================
# SiteProfile — ScoutAgent 输出
# ============================================================================

class SiteProfile(BaseModel):
    """
    站点画像。ScoutAgent 分析首页后输出，供其他 Agent 决策使用。
    """
    url: str = Field(..., description="种子/首页 URL")
    title: str = Field(default="", description="站点标题 (<title> 标签)")
    needs_js_render: bool = Field(
        default=False,
        description="是否强依赖 JS 渲染 (首页 requests 拿回的文本 < 500 字节 → True)"
    )
    site_type: str = Field(
        default="https://www.baidu.com",
        description="网站类型分类: cms / spa / portal / blog / ecommerce / other"
    )
    anti_crawl_level: str = Field(
        default="low",
        description="反爬强度评估: low / medium / high"
    )
    encoding: str = Field(default="utf-8", description="页面编码")
    has_captcha: bool = Field(default=False, description="是否检测到验证码/人机校验")
    extra: Dict[str, Any] = Field(default_factory=dict, description="扩展字段")

    class Config:
        frozen = False


# ============================================================================
# NavLink — NavAgent 解析出的单条链接
# ============================================================================

class NavLink(BaseModel):
    """
    NavAgent 从导航栏解析出的单条链接，携带有层级路径和页面类型判定。
    """
    url: str = Field(..., description="目标 URL (已补全为完整 URL)")
    text: str = Field(default="", description="链接文本 / 导航栏标签")
    nav_path: List[str] = Field(
        default_factory=list,
        description="导航层级路径，如 ['新闻中心', '行业动态']；长度为 depth"
    )
    depth: int = Field(
        default=1, ge=1, le=4,
        description="导航层级深度 1-4，基于导航栏 DOM 层级判定"
    )
    is_detail_page: bool = Field(
        default=True,
        description="True=详情页(保留), False=列表页(过滤丢弃)"
    )
    is_homepage: bool = Field(
        default=False,
        description="True=首页链接(过滤丢弃)"
    )
    link_density_score: float = Field(
        default=0.0,
        description="链接密度评分 0-1，越高越可能是列表页"
    )
    source_nav_element: str = Field(
        default="",
        description="来源导航元素的 DOM 路径/选择器 (调试用)"
    )

    class Config:
        frozen = False


# ============================================================================
# PageData — Agent 间流转的核心数据对象
# ============================================================================

class PageData(BaseModel):
    """
    抓取/清洗过程中流转的核心数据对象。
    FetcherRouter 产出(html 原样) → ExtractorAgent 清洗(html 仅正文) → StorageManager 落盘
    """
    url: str = Field(..., description="页面 URL")
    title: str = Field(default="", description="页面标题 (<title> 或正文提取)")
    html: str = Field(default="", description="HTML 内容 (ExtractorAgent 后为清洗后正文)")
    raw_html: str = Field(default="", description="原始未清洗 HTML (调试/回溯用)")
    nav_path: List[str] = Field(
        default_factory=list,
        description="导航层级路径, 如 ['关于我们', '公司简介']"
    )
    depth: int = Field(default=1, ge=0, le=4, description="导航深度 0-4")
    fetch_method: str = Field(
        default="",
        description="抓取方式: httpx / playwright / cached"
    )
    content_quality_score: float = Field(
        default=1.0,
        description="内容质量评分 0-1, ExtractorAgent 判定"
    )
    is_list_page_detected_at_extract: bool = Field(
        default=False,
        description="ExtractorAgent 二次拦截: True=误抓列表页, 应丢弃"
    )
    images_count: int = Field(default=0, ge=0, description="正文图片数量")
    images_urls: List[str] = Field(default_factory=list, description="正文图片 URL 列表")
    images_alts: List[str] = Field(default_factory=list, description="图片 Alt 文本列表")
    content_hash: str = Field(default="", description="清洗后纯文本 MD5 指纹(去重用)")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="抓取时间戳 ISO 格式"
    )
    extra: Dict[str, Any] = Field(default_factory=dict, description="扩展字段")

    class Config:
        frozen = False


# ============================================================================
# CrawlResult — StorageManager 落盘后的元数据 (写入 CSV)
# ============================================================================

class CrawlResult(BaseModel):
    """
    单条爬取结果，对应目标 CSV 模板字段（12 列，严格固定顺序）。
    """
    # ── CSV 模板字段（严格按此顺序写入 CSV） ──
    sys_platfuuid: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="系统唯一标识 UUID"
    )
    brwidcl_cpmc: str = Field(default="", description="目标网站名称 (首页 title)")
    ywlx: str = Field(default="", description="完整导航路径 (如 新闻中心/行业动态)")
    ywlx1: str = Field(default="", description="一级导航名称")
    ywlx2: str = Field(default="", description="二级导航名称")
    ywlx3: str = Field(default="", description="三级导航名称")
    ywlx4: str = Field(default="", description="四级导航名称")
    tianextimejsj: str = Field(default="", description="抓取时间 (YYYY-MM-DD HH:MM:SS)")
    title: str = ""
    html: str = Field(default="", description="清洗后的正文 HTML")
    download_img_url: str = Field(default="", description="图片绝对 URL (多个用 ; 分隔)")
    img_title: str = Field(default="", description="图片 Alt 文本 (多个用 ; 分隔)")

    # ── 内部追踪字段（不写入 CSV） ──
    url: str = ""
    status: str = Field(default="success")
    error_message: str = Field(default="")
    file_path: str = Field(default="")
    content_hash: str = Field(default="")

    class Config:
        frozen = False
