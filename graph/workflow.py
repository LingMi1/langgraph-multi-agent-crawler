"""
graph/workflow.py — LangGraph StateGraph 组装与编译。

图结构:
  START
    │
    ▼
  scout_node ─── 分析站点
    │
    ▼
  navigate_node ─── 提取导航链接 → 填充 queue
    │
    ▼
  fetch_extract_node ←────────────┐
    │                              │
    ▼                              │
  [route_after_fetch]              │
    │                              │
    ├── queue 非空 ───────────────┘  (loop)
    │
    └── queue 为空
         │
         ▼
  evaluate_node (LLM) ─── 评估爬取质量
    │
    ▼
  [route_after_evaluate]
    │
    ├── passed=true ──────────► storage_node → END
    │
    ├── passed=false + 调整<3 → config_adjust_node → navigate_node (重抓)
    │
    ├── passed=false + 调整≥3 + 未生成规则 → code_gen_node (LLM 最后保底)
    │        │
    │        └── navigate_node (用新规则重抓)
    │
    └── passed=false + 调整≥3 + 已生成规则 → storage_node → END
"""

from __future__ import annotations

from typing import Optional, Callable

from langgraph.graph import StateGraph, END, START

from .state import CrawlerState
from .nodes import (
    scout_node,
    navigate_node,
    fetch_extract_node,
    evaluate_node,
    config_adjust_node,
    code_gen_node,
    media_processor_node,
    storage_node,
    route_after_fetch,
    route_after_evaluate,
)

from schemas import agent_logger


# ============================================================================
# 图构建
# ============================================================================

def build_crawler_graph() -> StateGraph:
    """
    构建多 Agent 爬虫的 LangGraph StateGraph。

    Returns:
        编译后的 StateGraph（可直接 invoke）
    """
    # 1. 创建 StateGraph
    graph = StateGraph(CrawlerState)

    # 2. 添加节点
    graph.add_node("scout_node", scout_node)
    graph.add_node("navigate_node", navigate_node)
    graph.add_node("fetch_extract_node", fetch_extract_node)
    graph.add_node("evaluate_node", evaluate_node)
    graph.add_node("config_adjust_node", config_adjust_node)
    graph.add_node("code_gen_node", code_gen_node)
    graph.add_node("media_processor_node", media_processor_node)
    graph.add_node("storage_node", storage_node)

    # 3. 添加边
    # ── 主线：START → scout → navigate → fetch_extract ──
    graph.add_edge(START, "scout_node")
    graph.add_edge("scout_node", "navigate_node")
    graph.add_edge("navigate_node", "fetch_extract_node")

    # ── 条件路由：fetch_extract 之后 ──
    graph.add_conditional_edges(
        "fetch_extract_node",
        route_after_fetch,
        {
            "fetch_extract_node": "fetch_extract_node",  # 循环
            "evaluate_node": "evaluate_node",            # 评估
            "storage_node": "storage_node",              # 出错直接落盘
        },
    )

    # ── 条件路由：评估之后 ──
    graph.add_conditional_edges(
        "evaluate_node",
        route_after_evaluate,
        {
            "media_processor_node": "media_processor_node",  # 通过/放弃 → 媒体处理
            "config_adjust_node": "config_adjust_node",      # 调整重来
            "code_gen_node": "code_gen_node",                # LLM 最后保底
            "storage_node": "storage_node",                  # 出错直接落盘
        },
    )

    # ── 调整后回到导航 ──
    graph.add_edge("config_adjust_node", "navigate_node")

    # ── LLM 规则生成后回到导航（用新规则重新抓取） ──
    graph.add_edge("code_gen_node", "navigate_node")

    # ── 媒体处理后落盘 ──
    graph.add_edge("media_processor_node", "storage_node")

    # ── 存储后结束 ──
    graph.add_edge("storage_node", END)

    return graph


# ============================================================================
# 运行时入口
# ============================================================================

# 编译图（模块级单例，避免每次 invoke 重新编译）
_crawler_app: Optional[any] = None


def get_crawler_app():
    """获取编译后的 LangGraph 应用（懒加载单例）"""
    global _crawler_app
    if _crawler_app is None:
        graph = build_crawler_graph()
        _crawler_app = graph.compile()
        agent_logger.info("[Graph::workflow] LangGraph 爬虫工作流已编译")
    return _crawler_app


async def run_crawler(
    seed_url: str,
    log_callback: Optional[Callable[[str], None]] = None,
    max_steps: int = 20000,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    concurrency: int = 3,
) -> dict:
    """
    运行 LangGraph 爬虫工作流。

    Args:
        seed_url:     种子 URL（站点首页）
        log_callback: 日志回调（可选，供 GUI 使用）
        max_steps:    最大图执行步数（防止无限循环）。默认 20000：
                      深层 BFS + 分页发现时单页列表可能消耗大量 step，
                      2000 会在队列尚未排空时被截断，导致 stats 全部丢失。
                      （仍设上限以防死循环）
        progress_callback: 进度回调 (fetched, queue_len, url, phase)，
                          phase ∈ {"fetch","media","storage"}，每处理一页调用一次
        concurrency:  fetch_extract 节点每批并发处理的 URL 数（BFS 批次内 asyncio.gather 并发）
    Returns:
        最终状态中的 stats 字典
    """
    def log(msg: str) -> None:
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    log(f"启动 LangGraph 多 Agent 爬虫 | 目标: {seed_url} | max_steps={max_steps} | concurrency={concurrency}")

    app = get_crawler_app()
    initial_state: CrawlerState = {
        "seed_url": seed_url,
        "progress_callback": progress_callback,
        "concurrency": max(1, int(concurrency or 1)),
    }

    try:
        # LangGraph 的 ainvoke 会按图结构逐步执行
        # config 中的 recursion_limit 控制最大步数，防止死循环
        final_state = await app.ainvoke(
            initial_state,
            config={"recursion_limit": max_steps},
        )
    except Exception as e:
        import traceback
        import os as _os
        log(f"❌ LangGraph 工作流异常: {e}")
        log(traceback.format_exc())
        # 兜底：异常中断时从磁盘统计已保存文件数，避免 stats 全部丢失
        # （输出目录与 scout_node 保持一致: output/<netloc>）
        try:
            from urllib.parse import urlparse as _up
            from config import LOCAL_BACKUP_DIR as _out_root
            _dom = _up(seed_url).netloc.replace(":", "_")
            _od = _os.path.join(_out_root, _dom)
            _n = (sum(1 for _, _, fs in _os.walk(_od) for f in fs if f.endswith(".html"))
                  if _os.path.isdir(_od) else 0)
            if _n > 0:
                log(f"  📁 (异常恢复: 磁盘已有 {_n} 个 HTML 文件)")
            return {"pages_saved": _n, "saved": _n, "error": str(e)}
        except Exception:
            return {"pages_saved": 0, "error": str(e)}

    stats = final_state.get("stats", {})
    evaluation = final_state.get("evaluation", {})
    adjustment_count = final_state.get("adjustment_count", 0)
    error = final_state.get("error", "")
    blocked_urls = final_state.get("anti_crawl_blocked_urls", {})

    log(f"\n{'='*50}")
    log(f"📊 LangGraph 爬虫完成")
    log(f"  侦察:       {stats.get('scouted', 0)}")
    log(f"  抓取:       {stats.get('fetched', 0)}")
    log(f"  清洗:       {stats.get('extracted', 0)}")
    log(f"  保存:       {stats.get('saved', 0)}")
    log(f"  跳过:       {stats.get('skipped', 0)}")
    log(f"  重复:       {stats.get('duplicate', 0)}")
    log(f"  失败:       {stats.get('failed', 0)}")
    if blocked_urls:
        log(f"  🛡️ 反爬拦截:  {len(blocked_urls)} 个 (高级反爬爬不了)")
        for uk, reason in list(blocked_urls.items())[:5]:
            log(f"     ⛔ {uk[:60]} — 高级反爬爬不了")
    log(f"  LLM评估:    {'通过' if evaluation.get('passed', True) else '未通过'} "
        f"(score={evaluation.get('score', 0):.2f})")
    log(f"  调整次数:   {adjustment_count}")
    if error:
        log(f"  ⚠️ 错误:    {error}")
    log(f"{'='*50}")

    result = dict(stats)
    result["anti_crawl_blocked_urls"] = blocked_urls
    return result
