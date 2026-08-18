"""
LangGraph 工作流 (Phase 3 重构: Supervisor + Worker 多智能体架构)

图结构:
  START → supervisor_node → {web_scraper?} → web_scraper_worker → supervisor_node (循环)
                         ↓
                       FINISH → END

新模块:
  - supervisor.py: LLM 驱动的 Supervisor 决策（JSON 输出 + 多层容错）
  - workers.py: ReAct Worker 循环（自纠错 + max_iterations + 可观测性）
  - state.py: AgentState + CSS 常量 + 可观测性日志

BFS 规则流 (bfs_app) 保留作为零 Token fallback。
"""

import json
from typing import Dict, Any

from langgraph.graph import StateGraph, END, START
from langgraph.graph.message import RemoveMessage
from langgraph.checkpoint.memory import MemorySaver

from state import AgentState, agent_logger
import supervisor as _supervisor
import workers as _worker

# 兼容旧引用
supervisor_node = _supervisor.supervisor_node

# ======================================================================
# Web Scraper Worker 子图（ReAct Agent）
# ======================================================================

def _build_web_scraper_subgraph() -> StateGraph:
    """
    构建 web_scraper Worker 的 ReAct 子图。
    
    ReAct 循环:
      worker_agent → {有 tool_calls?} → worker_tools → worker_agent
                     ↓
                    END → 返回 Supervisor
    """
    subgraph = StateGraph(AgentState)
    
    subgraph.add_node("worker_agent", _worker.worker_agent_node)
    subgraph.add_node("worker_tools", _worker.worker_tools_node)

    subgraph.set_entry_point("worker_agent")

    subgraph.add_conditional_edges(
        "worker_agent",
        _worker.worker_should_continue,
        {"worker_tools": "worker_tools", END: END}
    )

    subgraph.add_edge("worker_tools", "worker_agent")

    return subgraph.compile()


web_scraper_worker = _build_web_scraper_subgraph()


# ======================================================================
# Phase 3: 顶层路由函数
# ======================================================================

def supervisor_router(state: AgentState) -> str:
    """
    Supervisor 决策后的路由：
    - next_worker == "web_scraper" → web_scraper_worker
    - next_worker == "FINISH" 或空 → END
    """
    next_worker = state.get("next_worker", "FINISH")
    agent_logger.info(f"[Router] Supervisor 路由到: {next_worker}")
    if next_worker == "web_scraper":
        return "web_scraper"
    return END


def worker_router(state: AgentState) -> str:
    """
    Worker 执行完毕后，返回 Supervisor 重新决策。
    """
    # 汇总 Worker 结果
    extracted_data = state.get("extracted_data", [])
    worker_results = {
        "extracted_count": len(extracted_data),
        "status": "completed",
    }
    agent_logger.info(f"[Router] Worker 完成 → 返回 Supervisor | extracted={len(extracted_data)}")
    return "supervisor"


# ======================================================================
# Checkpointer
# ======================================================================

def get_checkpointer() -> MemorySaver:
    """获取 MemorySaver Checkpointer 实例"""
    return MemorySaver()


# ======================================================================
# 状态更新节点：Worker 完成后同步 url_queue / stats / visited
# ======================================================================

def post_worker_node(state: AgentState) -> Dict[str, Any]:
    """
    Post-Worker 状态同步节点。
    
    职责:
      1. 从 Worker 的 extracted_data / messages 中提取新发现的链接 → 追加到 url_queue
      2. 更新 stats（saved / success / failed）
      3. 将当前处理的 URL 标记为 visited
      4. 从 url_queue 头部移除已完成的 URL
    
    这解决了 Worker 发现的新链接无法回流到 Supervisor 的核心问题。
    """
    messages = state.get("messages", [])
    url_queue = list(state.get("url_queue", []))
    visited = list(state.get("visited", []))
    stats = dict(state.get("stats", {"total": 0, "success": 0, "failed": 0, "skipped": 0, "saved": 0}))
    extracted_data = state.get("extracted_data", [])
    worker_data = state.get("worker_data", {})
    
    current_url = worker_data.get("url", state.get("root_url", ""))
    # ★ 从 worker_data 中读取当前处理 URL 的深度信息（由 Supervisor 在 pop 时传入）
    current_page_depth = worker_data.get("depth", 1)
    current_page_nav_depth = worker_data.get("nav_depth", 1)
    current_page_breadcrumb = worker_data.get("breadcrumb", [])
    max_nav_depth = state.get("max_nav_depth", 4)
    
    # 1. 标记当前处理 URL 为 visited 并从 url_queue 移除
    if current_url:
        if current_url not in visited:
            visited.append(current_url)
        
        # 从 url_queue 中移除已处理的 URL
        url_queue = [q for q in url_queue if q.get("url", "") != current_url]
    
    # 2. 从 Worker 的 ToolMessage 中提取 extract_links 发现的新链接
    new_links_found = []
    fetch_success = True
    task_finished = False
    for msg in reversed(messages):
        content_str = msg.content if hasattr(msg, "content") else str(msg)
        # ToolMessage 的检测：content 中包含 total_found 字段表示 extract_links 结果
        try:
            parsed = json.loads(content_str)
            if isinstance(parsed, dict) and "new_links" in parsed and "total_found" in parsed:
                # 这是 extract_links 的输出
                new_links = parsed.get("new_links", [])
                existing_urls = {q.get("url", "") for q in url_queue}
                existing_urls.update(visited)
                for link in new_links:
                    link_url = link.get("url", "")
                    if link_url and link_url not in existing_urls:
                        # ★ 深度继承：新发现的链接深度 = 当前页面深度 + 1
                        new_nav_depth = current_page_nav_depth + 1
                        # 如果子链接导航深度超过上限，不再加入队列
                        if new_nav_depth > max_nav_depth:
                            continue
                        new_links_found.append({
                            "url": link_url,
                            "depth": current_page_depth + 1,
                            "nav_depth": new_nav_depth,
                            "breadcrumb": current_page_breadcrumb + [link.get("text", "")] if link.get("text") else current_page_breadcrumb,
                            "parent_url": current_url,
                        })
                        existing_urls.add(link_url)
            elif isinstance(parsed, dict) and "success" in parsed and "html_length" in parsed:
                # 这是 fetch_page 的输出
                fetch_success = parsed.get("success", False)
            elif isinstance(parsed, dict) and "status" in parsed and parsed.get("status") == "finished":
                # 这是 finish_task 的输出
                task_finished = True
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    
    # 将新链接加入 url_queue（去重）
    url_queue.extend(new_links_found)
    
    # 3. 更新统计
    stats["total"] = stats.get("total", 0) + 1
    if extracted_data:
        stats["saved"] = stats.get("saved", 0) + len(extracted_data)
    if fetch_success and (extracted_data or new_links_found):
        stats["success"] = stats.get("success", 0) + 1
    elif not fetch_success:
        stats["failed"] = stats.get("failed", 0) + 1
    
    agent_logger.info(
        f"[PostWorker] 同步完成 | "
        f"queue:{len(url_queue)} visited:{len(visited)} "
        f"saved:{stats['saved']} success:{stats['success']} failed:{stats['failed']} "
        f"new_links:{len(new_links_found)}"
    )
    
    # ★ 用 RemoveMessage 真正清空 messages（add_messages 是 append-only reducer，
    # "messages": [] 等于不追加，不等于清空。必须显式删除每条消息）
    msg_removals = [RemoveMessage(id=msg.id) for msg in messages if hasattr(msg, "id")]

    return {
        "url_queue": url_queue,
        "visited": visited,
        "stats": stats,
        "react_iteration": 0,
        "messages": msg_removals,
    }


# ======================================================================
# 构建 Supervisor + Worker 主工作流
# ======================================================================

def build_supervisor_workflow():
    """
    Phase 3: Supervisor + Worker 多智能体架构

    图结构:
        START → supervisor_node → {web_scraper?} → web_scraper_worker 
                              ↓                         ↓
                            FINISH → END           post_worker → supervisor_node (循环)

    未来扩展（只需添加新的 Worker 子图和路由）:
        supervisor_node → pdf_analyzer_worker → supervisor_node
        supervisor_node → data_qa_worker → supervisor_node
    """
    workflow = StateGraph(AgentState)

    # 注册 Supervisor
    workflow.add_node("supervisor", supervisor_node)

    # 注册 Worker 子图（作为黑盒节点）
    workflow.add_node("web_scraper", web_scraper_worker)
    
    # 注册 Post-Worker 状态同步节点
    workflow.add_node("post_worker", post_worker_node)

    workflow.set_entry_point("supervisor")

    # Supervisor → Worker / END
    workflow.add_conditional_edges(
        "supervisor",
        supervisor_router,
        {"web_scraper": "web_scraper", END: END}
    )

    # Worker → PostWorker → Supervisor（循环）
    workflow.add_conditional_edges(
        "web_scraper",
        worker_router,
        {"supervisor": "post_worker"}
    )
    
    # PostWorker → Supervisor
    workflow.add_edge("post_worker", "supervisor")

    return workflow.compile()


# 直接导出预编译工作流实例
app = build_supervisor_workflow()

# ======================================================================
# 🔧 BFS 工作流（LangGraph 模式 — 零 Token，不需要 API Key）
# ======================================================================

def _build_bfs_workflow():
    """
    BFS 全站爬取工作流（Phase 1 兼容）：
      full_site_fetch → image_rescue → content_clean → full_site_fetch (循环) → store → END
                    ↑                    ↓                 ↓
                    └── fallback ←───────┴─────────────────┘
    """
    import nodes
    from langgraph.graph import StateGraph, END as BFS_END
    from schemas import AgentState as BFSState

    wf = StateGraph(BFSState)
    wf.add_node("full_site_fetch", nodes.full_site_fetch_node)
    wf.add_node("image_rescue", nodes.image_rescue_node)
    wf.add_node("content_clean", nodes.content_clean_v2_node)
    wf.add_node("store_results", nodes.multi_level_store_node)
    wf.add_node("fallback", nodes.fallback_node)

    def _check_fatal(s: BFSState) -> bool:
        return s.get("fatal_error", False)

    def bf_after_fetch(s: BFSState) -> str:
        if _check_fatal(s): return "fallback"
        cp = s.get("current_page", {})
        if cp and cp.get("raw_html"): return "image_rescue"
        if s.get("url_queue", []): return "full_site_fetch"
        return "store_results"

    def bf_after_rescue(s: BFSState) -> str:
        if _check_fatal(s): return "fallback"
        cp = s.get("current_page", {})
        if cp and cp.get("raw_html"): return "content_clean"
        if s.get("url_queue", []): return "full_site_fetch"
        return "store_results"

    def bf_after_clean(s: BFSState) -> str:
        if _check_fatal(s): return "fallback"
        if s.get("url_queue", []): return "full_site_fetch"
        return "store_results"

    wf.set_entry_point("full_site_fetch")
    wf.add_conditional_edges("full_site_fetch", bf_after_fetch,
        {"image_rescue": "image_rescue", "full_site_fetch": "full_site_fetch",
         "store_results": "store_results", "fallback": "fallback"})
    wf.add_conditional_edges("image_rescue", bf_after_rescue,
        {"content_clean": "content_clean", "full_site_fetch": "full_site_fetch",
         "store_results": "store_results", "fallback": "fallback"})
    wf.add_conditional_edges("content_clean", bf_after_clean,
        {"full_site_fetch": "full_site_fetch", "store_results": "store_results", "fallback": "fallback"})
    wf.add_edge("store_results", BFS_END)
    wf.add_edge("fallback", BFS_END)
    return wf.compile()

bfs_app = _build_bfs_workflow()

agent_logger.info("Supervisor + Worker 多智能体工作流已编译（Phase 3: Multi-Agent 架构）")
agent_logger.info("BFS 工作流已就绪（LangGraph 模式，零 Token，不需要 API Key）")
