"""
graph/ — LangGraph 多 Agent 爬虫工作流

架构: 传统爬虫为主 + LLM 评估为辅
  - 传统爬虫（httpx/Playwright + trafilatura/BS4）始终是默认执行者
  - LLM 仅在传统爬虫完成后评估结果质量
  - 如需调整，LLM 建议配置变更（UA/JS渲染/Headers），不生成代码

模块:
  - state.py:    CrawlerState TypedDict + EvaluationResult Pydantic 模型
  - nodes.py:    6 个 Graph 节点（scout / navigate / fetch_extract / evaluate / config_adjust / storage）
  - workflow.py: StateGraph 组装 + run_crawler 入口
"""

from .workflow import build_crawler_graph, get_crawler_app, run_crawler
from .state import CrawlerState, EvaluationResult, QualityIssue, CrawlerConfig, ExtractionRules, GeneratedRule

__all__ = [
    "build_crawler_graph",
    "get_crawler_app",
    "run_crawler",
    "CrawlerState",
    "EvaluationResult",
    "QualityIssue",
    "CrawlerConfig",
    "ExtractionRules",
    "GeneratedRule",
]
