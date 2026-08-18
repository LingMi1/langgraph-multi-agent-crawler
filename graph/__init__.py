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

# ── 向后兼容：重新导出旧 graph.py 模块的符号 ──
# 因为 graph/ 包会遮蔽同目录的 graph.py，需要在此代理导入
import os as _os
import importlib.util as _util

_parent_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_old_graph_path = _os.path.join(_parent_dir, "graph.py")
if _os.path.exists(_old_graph_path):
    _spec = _util.spec_from_file_location("_original_graph_module", _old_graph_path)
    _orig = _util.module_from_spec(_spec)
    _spec.loader.exec_module(_orig)
    # 重新导出
    app = _orig.app
    get_checkpointer = _orig.get_checkpointer
    bfs_app = _orig.bfs_app
    supervisor_node = _orig.supervisor_node

__all__ = [
    # 新架构
    "build_crawler_graph",
    "get_crawler_app",
    "run_crawler",
    "CrawlerState",
    "EvaluationResult",
    "QualityIssue",
    "CrawlerConfig",
    "ExtractionRules",
    "GeneratedRule",
    # 旧兼容
    "app",
    "get_checkpointer",
    "bfs_app",
    "supervisor_node",
]
