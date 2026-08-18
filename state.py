"""
项目全局状态定义 + 常量（从 schemas.py 和 nodes.py 抽取）
"""
import logging
from typing import TypedDict, List, Dict, Any, Optional, Annotated
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field, field_validator


# ======================================================================
# Pydantic 输出校验模型
# ======================================================================

class NewsArticleSchema(BaseModel):
    url: str = Field(..., description="原始页面 URL")
    title: str = Field(..., min_length=1, max_length=500, description="文章标题")
    publish_time: Optional[str] = Field(default=None, description="发布时间 ISO 格式")
    breadcrumb: List[str] = Field(default_factory=list)
    html_content: str = Field(default="", description="清洗后的 HTML 正文")
    images_count: int = Field(default=0, ge=0)
    nav_levels: List[str] = Field(default_factory=lambda: ["", "", "", ""])

    @field_validator("url")
    @classmethod
    def url_must_be_valid(cls, v: str) -> str:
        if not v or not v.startswith(("http://", "https://")):
            raise ValueError(f"无效 URL: {v[:80]}")
        return v

    @field_validator("title")
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("标题不能为空")
        return stripped


class ValidationResult(BaseModel):
    is_valid: bool
    article: Optional[NewsArticleSchema] = None
    error_reason: Optional[str] = None
    raw_data: Optional[Dict[str, Any]] = None


# ======================================================================
# AgentState
# ======================================================================

class AgentState(TypedDict, total=False):
    root_url: str
    base_url: str
    max_depth: int
    max_nav_depth: int
    current_depth: int
    visited: List[str]
    url_queue: List[Dict[str, Any]]
    current_page: Dict[str, Any]
    results: List[Dict[str, Any]]
    stats: Dict[str, int]
    error: str
    nav_mapping: Dict[str, str]

    current_url: str
    extracted_data: List[Dict[str, Any]]
    error_log: List[Dict[str, Any]]
    token_usage: Dict[str, Any]
    retry_count: int
    fatal_error: bool
    node_consecutive_failures: Dict[str, int]

    messages: Annotated[List[BaseMessage], add_messages]
    task_complete: bool
    hitl_interrupt: bool
    hitl_reason: str

    next_worker: str
    worker_data: Dict[str, Any]
    worker_results: Dict[str, Any]
    supervisor_messages: Annotated[List[BaseMessage], add_messages]

    # Worker ReAct 循环计数
    react_iteration: int


# ======================================================================
# 日志
# ======================================================================

def setup_logger(name: str = "agent_crawler") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


agent_logger = setup_logger()


# ======================================================================
# CSS 常量（供清洗函数使用）
# ======================================================================

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
html, body { margin: 0; padding: 0; }
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
img { max-width: 100% !important; height: auto !important; display: block !important; margin: 12px auto !important; }
video { max-width: 100%; display: block; margin: 12px auto; }
ul, ol { font-size: 16px; line-height: 2; margin: 8px 0; padding-left: 2em; }
li { margin-bottom: 5px; }
table { max-width: 100%; border-collapse: collapse; margin: 12px auto; }
table td, table th { border: 1px solid #ddd; padding: 8px; font-size: 14px; }
a { color: #1a73e8; text-decoration: underline; word-break: break-all; }
.row, [class*="col-"], .flex, [class*="flex"] { display: block !important; flex: none !important; flex-wrap: nowrap !important; }
@media (max-width: 767px) { body { padding: 10px; } }
"""


# ======================================================================
# 可观测性日志
# ======================================================================

_REACT_LOG_HEADER = "=" * 60

def log_agent_thought(role: str, type_: str, content: str, max_len: int = 300):
    """格式化 Agent 思考过程日志"""
    c = content[:max_len] + ("..." if len(content) > max_len else "")
    lines = c.split("\n")[:5]
    prefix_map = {
        ("Supervisor", "thought"): "  🧠 [Supervisor] 决策: ",
        ("Supervisor", "action"): "  📋 [Supervisor] 动作: ",
        ("Worker", "thought"):    "  🤔 [Worker]   思考: ",
        ("Worker", "action"):     "  ⚡ [Worker]   动作: ",
        ("Worker", "observation"):"  👁  [Worker]   观察: ",
        ("Worker", "error"):      "  ❌ [Worker]   错误: ",
    }
    prefix = prefix_map.get((role, type_), f"  [{role}] {type_}: ")
    print(prefix + lines[0])
    for line in lines[1:]:
        print(" " * len(prefix.rstrip(prefix.split(':')[0]) + ": ") + line)


def log_section(title: str):
    print(f"\n{_REACT_LOG_HEADER}")
    print(f"  {title}")
    print(f"{_REACT_LOG_HEADER}")