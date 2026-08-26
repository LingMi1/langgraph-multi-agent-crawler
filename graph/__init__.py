"""
graph/ — LangGraph 多 Agent 爬虫工作流（Supervisor 编排）

架构: 传统爬虫为主 + LLM 评估为辅（确定性优先、成本分层）
  - 编排级 Agent（graph/agents.py）：8 个 Agent = 8 个图节点，统一走 BaseAgent.run()
    （轨迹记录 + 异常隔离）
  - 能力级 Agent（agents/*.py）：单页级工具（scout / nav / fetcher / extractor / storage）
  - workflow 是监督者（Supervisor）：按图编排子 Agent，EvaluateAgent 是审查者
    决定任务交给 AdjustAgent / CodeGenAgent 还是放行存储

模块:
  - state.py:    CrawlerState TypedDict + EvaluationResult Pydantic 模型
  - agents.py:   编排级 Agent 实现（Scout/Navigate/FetchExtract/Evaluate/Adjust/CodeGen/Media/Storage）
  - nodes.py:    节点核心逻辑（被编排级 Agent 包装）
  - workflow.py: StateGraph 组装 + run_crawler 入口
"""

from .workflow import build_crawler_graph, build_app, run_crawler
from .agents import build_agents
from .state import CrawlerState, EvaluationResult, QualityIssue, CrawlerConfig, ExtractionRules, GeneratedRule
from agents.base import AgentContext, BaseAgent, TraceRecorder

__all__ = [
    "build_crawler_graph",
    "build_app",
    "run_crawler",
    "build_agents",
    "AgentContext",
    "BaseAgent",
    "TraceRecorder",
    "CrawlerState",
    "EvaluationResult",
    "QualityIssue",
    "CrawlerConfig",
    "ExtractionRules",
    "GeneratedRule",
]
