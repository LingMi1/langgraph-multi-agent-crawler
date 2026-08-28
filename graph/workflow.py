"""
graph/workflow.py — LangGraph StateGraph 组装与编译（Supervisor 多智能体编排）。

架构定位：
  workflow 是监督者（Supervisor），负责把 8 个编排级 Agent 组织成图并路由：
    ScoutAgent → NavigateAgent → FetchExtractAgent → EvaluateAgent
      → (通过) MediaProcessorAgent → StorageAgent
      → (不通过) ConfigAdjustAgent / CodeGenAgent → NavigateAgent 重抓

图结构:
  START
    │
    ▼
  scout ─── 侦察 + 产出任务计划(plan)
    │
    ▼
  navigate ─── 提取导航链接 → 填充 queue + 栏目清单并入 plan
    │
    ▼
  fetch_extract ←────────────┐
    │                         │
    ▼                         │
  [route_after_fetch]         │
    │                         │
    ├── queue 非空 ──────────┘  (loop)
    │
    └── queue 为空
         │
         ▼
  evaluate ─── 审查：评估质量 + 对照 plan 检查完成度
    │
    ▼
  [route_after_evaluate]
    ├── passed=true ─────────► media → storage → END
    ├── passed=false + 调整<3 → config_adjust → navigate (重抓)
    ├── passed=false + 调整≥3 + 未生成规则 → code_gen (LLM 最后保底) → navigate
    └── passed=false + 已生成规则 → react (深降级 ReAct 接管)
            ├── retry  → navigate (用新配置重抓)
            └── giveup → media → storage → END

每个节点的执行体是 Agent.run()（agents/base.py 模板方法）：
轨迹记录（trace JSONL）+ 异常隔离（degraded 降级不打断整图）。
"""

from __future__ import annotations

import os
import time
from typing import Dict, Optional, Callable

from langgraph.graph import StateGraph, END, START

from .state import CrawlerState
from .agents import build_agents
from .nodes import (
    route_after_fetch,
    route_after_evaluate,
    route_after_react,
)
from agents.base import AgentContext, BaseAgent, TraceRecorder
from config import LOCAL_BACKUP_DIR
from schemas import agent_logger

# 节点名（保持与路由字符串一致）
_AGENT_NODE_MAP: Dict[str, str] = {
    "scout": "scout_node",
    "navigate": "navigate_node",
    "fetch_extract": "fetch_extract_node",
    "evaluate": "evaluate_node",
    "config_adjust": "config_adjust_node",
    "code_gen": "code_gen_node",
    "react": "react_node",
    "media_processor": "media_processor_node",
    "storage": "storage_node",
}


# ============================================================================
# 图构建（Supervisor 编排）
# ============================================================================

def build_crawler_graph(agents: Dict[str, BaseAgent]) -> StateGraph:
    """
    构建多 Agent 爬虫的 LangGraph StateGraph（Supervisor 模式）。

    Args:
        agents: build_agents(ctx) 产出的编排级 Agent 实例表（按 agent.name 索引）

    Returns:
        编译前的 StateGraph（调用方 compile）
    """
    graph = StateGraph(CrawlerState)

    # ── 注册节点：每个节点 = 一个 Agent 的统一入口 run() ──
    for agent_name, node_name in _AGENT_NODE_MAP.items():
        agent = agents.get(agent_name)
        if agent is None:
            raise KeyError(f"缺少编排级 Agent: {agent_name}")
        graph.add_node(node_name, agent.run)

    # ── 主线：START → scout → navigate → fetch_extract ──
    graph.add_edge(START, "scout_node")
    graph.add_edge("scout_node", "navigate_node")
    graph.add_edge("navigate_node", "fetch_extract_node")

    # ── 条件路由：fetch_extract 之后（BFS 循环） ──
    graph.add_conditional_edges(
        "fetch_extract_node",
        route_after_fetch,
        {
            "fetch_extract_node": "fetch_extract_node",  # 循环
            "evaluate_node": "evaluate_node",            # 评估
            "storage_node": "storage_node",              # 出错直接落盘
        },
    )

    # ── 条件路由：评估之后（审查者裁决） ──
    graph.add_conditional_edges(
        "evaluate_node",
        route_after_evaluate,
        {
            "media_processor_node": "media_processor_node",  # 通过/放弃 → 媒体处理
            "config_adjust_node": "config_adjust_node",      # 调整重来
            "code_gen_node": "code_gen_node",                # LLM 最后保底
            "react_node": "react_node",                      # 深降级 ReAct 自主接管
            "storage_node": "storage_node",                  # 出错直接落盘
        },
    )

    # ── 调整 / 规则生成后回到导航（用新配置/规则重抓） ──
    graph.add_edge("config_adjust_node", "navigate_node")
    graph.add_edge("code_gen_node", "navigate_node")

    # ── 深降级接管后：重试 → 导航重抓；放弃 → 媒体处理落盘 ──
    graph.add_conditional_edges(
        "react_node",
        route_after_react,
        {
            "navigate_node": "navigate_node",          # 接管决策 retry → 用新配置重抓
            "media_processor_node": "media_processor_node",  # 接管决策 giveup → 落盘
        },
    )

    # ── 媒体处理后落盘 ──
    graph.add_edge("media_processor_node", "storage_node")

    # ── 存储后结束 ──
    graph.add_edge("storage_node", END)

    return graph


# ============================================================================
# 运行时入口
# ============================================================================

def build_app(seed_url: str, concurrency: int = 3) -> "tuple[Any, AgentContext]":
    """
    构建一次运行所需的 (编译图, AgentContext)。

    每次 run_crawler 独立构建（LangGraph 编译开销可忽略）：
      - TraceRecorder 仅内存记账（persist=False），不落盘 traces/trace_*.jsonl
      - 8 个编排级 Agent 共享同一 AgentContext（trace / llm / memory）
    """
    from urllib.parse import urlparse

    netloc = urlparse(seed_url).netloc.replace(":", "_")
    output_dir = str(LOCAL_BACKUP_DIR)
    trace = TraceRecorder(
        output_dir=os.path.join(output_dir, netloc),
        run_id=time.strftime("%Y%m%d_%H%M%S"),
        persist=False,  # ★ 不落盘 JSONL（不创建 traces/ 目录、不写 trace_*.jsonl）
    )
    ctx = AgentContext(trace=trace)
    agents = build_agents(ctx)
    graph = build_crawler_graph(agents)
    app = graph.compile()
    agent_logger.info(
        f"[Graph::workflow] Supervisor 图已编译 | trace={'落盘' if trace.path else '(不落盘)'}"
    )
    return app, ctx


async def run_crawler(
    seed_url: str,
    log_callback: Optional[Callable[[str], None]] = None,
    max_steps: int = 20000,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    concurrency: int = 3,
) -> dict:
    """
    运行 LangGraph 多 Agent 爬虫工作流（Supervisor 编排）。

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

    # ★ 运行级熔断复位：每个 run 白纸启动（上一 run 的端点故障不带入本 run）
    try:
        from agents.breaker import llm_breaker
        llm_breaker.reset()
    except Exception:
        pass

    app, ctx = build_app(seed_url, concurrency)
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
    plan = final_state.get("plan", {}) or {}

    log(f"\n{'='*50}")
    log(f"📊 LangGraph 爬虫完成")
    log(f"  侦察:       {stats.get('scouted', 0)}")
    log(f"  抓取:       {stats.get('fetched', 0)}")
    log(f"  清洗:       {stats.get('extracted', 0)}")
    log(f"  保存:       {stats.get('saved', 0)}")
    log(f"  跳过:       {stats.get('skipped', 0)}")
    log(f"  重复:       {stats.get('duplicate', 0)}")
    log(f"  失败:       {stats.get('failed', 0)}")
    if stats.get("rescue_candidates"):
        log(f"  批量抢救:   候选={stats.get('rescue_candidates', 0)} | "
            f"成功={stats.get('rescued', 0)} | 降级保存={stats.get('rescue_degraded', 0)} | "
            f"跳过={stats.get('rescue_skipped', 0) + stats.get('rescue_dup', 0)}")
    try:
        from agents.breaker import llm_breaker
        if llm_breaker.open:
            log(f"  熔断:       LLM 本 run 已熔断 | {llm_breaker.reason[:80]}")
    except Exception:
        pass
    if blocked_urls:
        log(f"  🛡️ 反爬拦截:  {len(blocked_urls)} 个 (高级反爬爬不了)")
        for uk, reason in list(blocked_urls.items())[:5]:
            log(f"     ⛔ {uk[:60]} — 高级反爬爬不了")
    log(f"  LLM评估:    {'通过' if evaluation.get('passed', True) else '未通过'} "
        f"(score={evaluation.get('score', 0):.2f})")
    log(f"  调整次数:   {adjustment_count}")
    if plan:
        log(f"  任务计划:   type={plan.get('site_type')} | "
            f"栏目={len(plan.get('expected_sections') or [])} | 状态={plan.get('status')}")
    if error:
        log(f"  ⚠️ 错误:    {error}")
    # ★ 成本记账：汇总本次运行的 LLM 调用与 token 估算
    try:
        from graph.nodes import get_budget_summary
        budget = get_budget_summary()
        if budget:
            log(f"  💰 Token预算: {budget}")
    except Exception:
        pass
    if ctx.trace.path:
        log(f"  🧭 多Agent轨迹: {ctx.trace.path}")
    log(f"{'='*50}")

    result = dict(stats)
    result["anti_crawl_blocked_urls"] = blocked_urls
    return result
