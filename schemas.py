"""
Pydantic 数据模型 + LangGraph AgentState 定义 (Phase 1 企业级升级)

包含:
  - NewsArticleSchema: 期望提取的数据结构（需求4：Guardrails）
  - AgentState: LangGraph TypedDict（需求1：强化 State）
"""

import logging
import os
from typing import TypedDict, List, Dict, Any, Optional, Union, Annotated
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field, field_validator
from datetime import datetime


# ======================================================================
# 🔴 需求 4：Pydantic 输出校验模型
# ======================================================================

class NewsArticleSchema(BaseModel):
    """
    期望从每个页面提取的新闻文章数据结构。

    所有提取结果必须通过 .model_validate() 校验，
    校验失败的数据被标记为"脏数据"并记录原因。
    """
    url: str = Field(..., description="原始页面 URL")
    title: str = Field(..., min_length=1, max_length=500, description="文章标题（必填）")
    publish_time: Optional[str] = Field(default=None, description="发布时间 ISO 格式")
    breadcrumb: List[str] = Field(default_factory=list, description="面包屑导航层级")
    html_content: str = Field(default="", description="清洗后的 HTML 正文")
    images_count: int = Field(default=0, ge=0, description="图片数量")
    nav_levels: List[str] = Field(default_factory=lambda: ["", "", "", ""],
                                  description="1-4 级导航栏名称 [ywlx1, ywlx2, ywlx3, ywlx4]")

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
    """单条数据校验结果"""
    is_valid: bool
    article: Optional[NewsArticleSchema] = None
    error_reason: Optional[str] = None
    raw_data: Optional[Dict[str, Any]] = None  # 保留原始数据用于调试


# ======================================================================
# 🔴 需求 1：重新定义 AgentState — 企业级状态
# ======================================================================

class AgentState(TypedDict, total=False):
    """
    企业级 Agent 全局状态（Partial Update 兼容）。
    total=False 表示所有字段均为可选，节点只返回需要更新的字段。

    新增字段（Phase 1）:
      - current_url:        当前正在处理的 URL
      - extracted_data:     已提取并通过校验的数据列表 List[NewsArticleSchema]
      - error_log:          错误日志列表 List[Dict]
      - token_usage:        累计 Token 消耗 {"prompt": int, "completion": int, "model": str}
      - retry_count:        当前节点重试次数
      - fatal_error:        致命错误标志（触发 fallback_node）
      - node_consecutive_failures: 各节点连续失败计数 Dict[str, int]
    """

    # ===== 原有字段（保持兼容）=====
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

    # ===== Phase 1 新增字段 =====
    current_url: str                                          # 当前处理 URL
    extracted_data: List[Dict[str, Any]]                      # 通过校验的数据列表（序列化后的 NewsArticleSchema）
    error_log: List[Dict[str, Any]]                           # 错误日志 [{timestamp, node, url, error_type, message}]
    token_usage: Dict[str, Any]                               # {"prompt_tokens": 0, "completion_tokens": 0, "model": ""}
    retry_count: int                                          # 当前节点重试计数
    fatal_error: bool                                         # 致命错误标志
    node_consecutive_failures: Dict[str, int]                 # 各节点连续失败计数 {"full_site_fetch": 0, ...}

    # ===== Phase 2 (ReAct + HITL) 新增字段 =====
    messages: Annotated[List[BaseMessage], add_messages]      # ReAct Agent 对话历史
    task_complete: bool                                       # 任务是否已完成
    hitl_interrupt: bool                                      # 是否触发 HITL 中断
    hitl_reason: str                                          # HITL 中断原因

    # ===== Phase 3 (Supervisor + Multi-Worker) 新增字段 =====
    next_worker: str                                          # Supervisor 决定的下一个 Worker: "web_scraper" | "FINISH"
    worker_data: Dict[str, Any]                               # Supervisor 分发给 Worker 的任务参数
    worker_results: Dict[str, Any]                            # Worker 返回的执行结果汇总
    supervisor_messages: Annotated[List[BaseMessage], add_messages]  # Supervisor 的对话历史


# ======================================================================
# 🔴 需求 3：结构化日志配置
# ======================================================================

def setup_logger(name: str = "agent_crawler") -> logging.Logger:
    """创建结构化日志记录器，兼容 LangSmith/Langfuse"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        # 控制台输出
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        # 文件输出：详细日志落盘到项目根目录 crawler.log，便于 GUI 模式下排查
        try:
            log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crawler.log")
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception:
            # 文件日志不可用时不影响控制台输出
            pass

        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


# 全局 logger 实例
agent_logger = setup_logger()