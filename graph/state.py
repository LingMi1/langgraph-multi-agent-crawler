"""
graph/state.py — LangGraph 多 Agent 爬虫全局状态定义

架构原则:
  - 传统爬虫始终是默认执行者
  - LLM 仅在传统爬虫完成后介入评估
  - 状态字段分类清晰，每个节点只读写自己负责的部分
"""

from typing import TypedDict, List, Dict, Any, Optional, Annotated, Callable
from pydantic import BaseModel, Field
import operator


# ============================================================================
# 辅助 Reducer：让 LangGraph 知道如何合并 List 字段
# ============================================================================

def _list_append(a: list, b: list) -> list:
    """追加合并器：langgraph 会自动把节点 return 的 list 追加到已有 list"""
    return (a or []) + (b or [])


def _dict_merge(a: dict, b: dict) -> dict:
    """浅合并：用 b 覆盖 a 中的同名字段"""
    merged = dict(a or {})
    merged.update(b or {})
    return merged


# ============================================================================
# Pydantic 辅助模型（用于 LLM JSON 输出解析）
# ============================================================================

class QualityIssue(BaseModel):
    """单个质量问题的描述"""
    type: str = Field(
        default="",
        description="问题类型: anti_crawl / content_quality / image_missing / coverage / other"
    )
    severity: str = Field(default="info", description="严重程度: info / warning / critical")
    description: str = Field(default="", description="问题描述")
    affected_pages: int = Field(default=0, description="受影响页面数")


class EvaluationResult(BaseModel):
    """LLM 评估节点的返回结构"""
    passed: bool = Field(default=True, description="整体是否通过")
    score: float = Field(default=1.0, ge=0.0, le=1.0, description="质量评分 0-1")
    issues: List[QualityIssue] = Field(default_factory=list, description="发现的问题列表")
    summary: str = Field(default="", description="一句话总结")
    suggestion: str = Field(default="", description="建议的修复措施")
    needs_js_render: bool = Field(default=False, description="是否需要启用 JS 渲染")
    recommended_ua: str = Field(default="", description="推荐的 User-Agent")
    recommended_headers: Dict[str, str] = Field(default_factory=dict, description="推荐的自定义请求头")


class CrawlerConfig(BaseModel):
    """爬虫运行时配置（可被 adjust_node 修改）"""
    user_agent: str = Field(default="", description="自定义 UA，空=使用默认池")
    needs_js_render: bool = Field(default=False, description="强制使用 Playwright 渲染")
    extra_headers: Dict[str, str] = Field(default_factory=dict, description="额外请求头")
    cookies: Dict[str, str] = Field(default_factory=dict, description="Cookie 键值对")
    request_delay: float = Field(default=1.0, ge=0.0, le=10.0, description="请求间延迟(秒)")
    use_system_chrome: bool = Field(default=False, description="是否使用系统安装的真实 Chrome 浏览器（而非 Playwright 自带 Chromium）")


class GeneratedRule(BaseModel):
    """LLM 生成的单条提取规则"""
    selector: str = Field(default="", description="CSS 选择器")
    purpose: str = Field(default="", description="用途: content / title / image / remove")
    note: str = Field(default="", description="LLM 的解释说明")


class ExtractionRules(BaseModel):
    """
    LLM 生成的站点定制提取规则集。

    不是 Python 代码，而是结构化配置，由现成的 BS4 引擎执行。
    安全性：只允许 CSS 选择器，禁止任意代码执行。
    """
    content_selectors: List[GeneratedRule] = Field(
        default_factory=list, description="正文容器选择器（按优先级排列）"
    )
    title_selectors: List[GeneratedRule] = Field(
        default_factory=list, description="标题选择器"
    )
    image_selectors: List[GeneratedRule] = Field(
        default_factory=list, description="图片选择器"
    )
    remove_selectors: List[GeneratedRule] = Field(
        default_factory=list, description="需移除的噪音元素选择器"
    )
    summary: str = Field(default="", description="LLM 对该站点结构的总结")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="LLM 对规则的信心")


# ============================================================================
# 全局状态 (TypedDict)
# ============================================================================

class CrawlerState(TypedDict, total=False):
    """
    贯穿整个 LangGraph 工作流的全局状态。

    每个节点返回包含部分字段的 dict，LangGraph 自动合并。
    使用 Annotated[...] 标注需要特殊 reducer 的字段。
    """

    # ── 输入 ──
    seed_url: str

    # ── 站点画像 ──
    site_profile: Dict[str, Any]      # SiteProfile 序列化
    site_name: str                     # 站点标题 → CSV brwidcl_cpmc
    output_dir: str                    # e.g. "output/www.example.com"

    # ── 一级导航映射（首页提取：导航名 → URL前缀，供 nav_path 吸附与 LLM 分类） ──
    nav_mapping: Dict[str, str]        # {"关于正邦": "/index.php/about/", ...}

    # ── 爬虫配置（可被 adjust_node 修改） ──
    crawler_config: Dict[str, Any]     # CrawlerConfig 序列化

    # ── 并发控制（fetch_extract_node 每批并发处理的 URL 数） ──
    concurrency: int

    # ── BFS 队列 ──
    queue: List[Dict[str, Any]]        # [{url, depth, nav_path}]
    seen_url_keys: List[str]           # 已处理/入队的 URL key
    url_retry_count: Dict[str, int]    # URL key → 已重试次数（防止无限重试）

    # ── 当前处理项 ──
    current_url: str
    current_depth: int
    current_nav_path: List[str]
    current_html: str
    current_title: str

    # ── 去重 ──
    seen_hashes: List[str]             # 已保存内容的 MD5
    global_seen_img_hashes: Dict[str, int]  # ★ 图片全局去重: MD5 → 出现次数

    # ── 媒体处理（图片内嵌） ──
    media_processed_urls: List[str]    # ★ 已内嵌 Base64 图片的页面 URL（防止 re-enqueue 覆盖）
    media_results: List[Dict[str, str]]  # ★ media 处理后的完整行（覆盖式，storage 优先读取）

    # ── 累积结果 ──
    crawled_results: Annotated[List[Dict[str, str]], _list_append]

    # ── LLM 评估 ──
    evaluation: Dict[str, Any]         # EvaluationResult 序列化
    adjustment_count: int              # 已调整次数（上限 3）

    # ── 多智能体任务计划（Plan-and-Execute：ScoutAgent 产出，EvaluateAgent 对照检查） ──
    plan: Dict[str, Any]               # {"status","steps","site_type","needs_js_render","template_hints","expected_sections"}

    # ── LLM 生成提取规则（最后保底） ──
    extraction_rules: Dict[str, Any]   # ExtractionRules 序列化（None=未生成）
    generation_attempted: bool         # 是否已尝试过 LLM 生成规则（防止死循环）
    failed_page_samples: List[str]     # 失败页面的 HTML 样本（供 LLM 分析）

    # ── 统计 ──
    stats: Dict[str, int]

    # ── 控制 ──
    error: str
    log_messages: Annotated[List[str], _list_append]
    progress_callback: Optional[Callable]  # ★ 进度回调: (fetched, queue_len, url)，每处理一页调用一次

    # ── 停用信号 ──
    anti_crawl_blocked_urls: Dict[str, str]        # URL key → 拦截原因（反爬降级）
    _stop_flag: bool                   # 外部停止信号
