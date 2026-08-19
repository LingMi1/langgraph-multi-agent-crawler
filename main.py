"""
============================================================================
  企业级 AI Agent 爬取工作流 (Phase 2: ReAct + HITL)
  基于 LangGraph + DeepSeek LLM 的 ReAct Agent：
    Agent 自主决策 → 调用 Tools → 检查结果 → 循环

  用法:
    python main.py                         # 命令行交互模式（含 HITL）
    from main import run_agent              # 供 GUI 调用

  特性:
    - LLM 自主决策工具调用（ReAct 循环）
    - 长期记忆：SQLite URL 去重
    - HITL 人工介入：批量保存 >1000 条 / Token 费用 >¥10 时中断
    - SQLite Checkpointer 持久化状态，支持 resume/abort
============================================================================
"""

import sys
import os
import json
import uuid
import asyncio
from typing import Callable, Optional
from threading import Event
from urllib.parse import urlparse

from graph import app, get_checkpointer
import config
from schemas import agent_logger
from memory import UrlMemory

# 多 Agent 流水线（旧版纯 async）
from agents.pipeline import CrawlerPipeline

# LangGraph 多 Agent 爬虫（新版）
from graph.workflow import run_crawler as run_langgraph_crawler_core

# 引入预检函数（统一网关）
import site_crawler


def _default_log(msg: str):
    """默认日志输出到 stdout"""
    print(msg)


def run_agent(target_url: str,
              log_callback: Optional[Callable[[str], None]] = None,
              stop_event: Optional[Event] = None,
              site_dir: Optional[str] = None,
              session_id: str = None,
              reset_memory: bool = False) -> int:
    """
    Phase 2: ReAct Agent 入口。

    参数:
      target_url:    目标网址（自动补全 https://）
      log_callback:  日志回调函数，接收 str 参数（用于 GUI 日志输出）
      stop_event:    停止信号
      site_dir:      可选，自定义输出根目录
      session_id:    会话 ID（用于断点续传，空则自动生成）

    返回:
      已保存的 HTML 页面数量（int）
    """
    log = log_callback or _default_log
    if stop_event is None:
        stop_event = Event()

    # 保存原始 BACKUP_DIR
    _original_backup_dir = config.LOCAL_BACKUP_DIR

    # ★ 重置 CSV Writer + 记忆库（每次新任务）
    import nodes
    nodes._reset_csv_writer()

    url = target_url.strip()
    if not url:
        log("❌ 网址不能为空")
        return 0
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    if not parsed.netloc:
        log(f"❌ 无法解析网址: {url}")
        return 0
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    if site_dir:
        config.LOCAL_BACKUP_DIR = site_dir

    # 生成或使用会话 ID
    if not session_id:
        session_id = f"agent_{uuid.uuid4().hex[:12]}"

    log(f"\n{'='*60}")
    log(f"  🤖 ReAct Agent 启动 (Phase 2)")
    log(f"  目标: {base_url}")
    log(f"  最大页面数: {config.MAX_PAGES}")
    log(f"  会话 ID: {session_id}")
    log(f"  输出目录: {config.LOCAL_BACKUP_DIR}")
    log(f"{'='*60}")

    if stop_event.is_set():
        log("⏹ 用户停止，跳过")
        config.LOCAL_BACKUP_DIR = _original_backup_dir
        return 0

    # ★ 重置入口：清空 SQLite 中当前站点的脏历史（短 HTML 缓存、URL 访问记录等）
    memory = UrlMemory()
    if reset_memory:
        deleted = memory.clear_site(base_url)
        log(f"🧹 已重置站点记忆: 清除 {deleted} 条记录（白纸启动）")

    # Phase 2 初始状态
    initial_state = {
        "root_url": url,
        "base_url": base_url,
        "max_depth": config.MAX_DEPTH,
        "max_nav_depth": config.MAX_NAV_DEPTH,
        "current_depth": 1,
        "visited": [],
        "url_queue": [
            {"url": url, "depth": 1, "nav_depth": 1, "breadcrumb": [], "parent_url": ""}
        ],
        "current_page": {},
        "results": [],
        "stats": {"total": 0, "success": 0, "failed": 0, "skipped": 0, "saved": 0},
        "error": "",
        "nav_mapping": {},
        # Phase 1
        "current_url": url,
        "extracted_data": [],
        "error_log": [],
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "model": ""},
        "retry_count": 0,
        "fatal_error": False,
        "node_consecutive_failures": {},
        # Phase 2 (ReAct + HITL)
        "messages": [],
        "task_complete": False,
        "hitl_interrupt": False,
        "hitl_reason": "",
        # Phase 3: ReAct Worker 循环计数
        "react_iteration": 0,
    }

    try:
        # 获取 Checkpointer 用于状态持久化
        checkpointer = get_checkpointer()

        # 构建 config（含 thread_id 用于 checkpoint）
        run_config = {
            "configurable": {
                "thread_id": session_id,
            }
        }

        # 使用 stream 模式配合 checkpointer
        saved_pages = 0
        final_state = None
        hitl_triggered = False
        hitl_reason = ""

        log(f"\n  🚀 Agent 开始执行...")
        log(f"  💡 提示: 当触发 HITL 中断时，输入 'resume' 继续，'abort' 终止\n")

        try:
            # 第一次执行
            for event in app.stream(initial_state, run_config):
                if stop_event.is_set():
                    log("⏹ 工作流收到停止信号，中断当前目标")
                    break

                final_state = event

                # 检查每个节点的输出看是否有 HITL 信号
                for node_name, node_output in event.items():
                    if isinstance(node_output, dict):
                        if node_output.get("hitl_interrupt"):
                            hitl_triggered = True
                            hitl_reason = node_output.get("hitl_reason", "未知原因")
                            break
                if hitl_triggered:
                    break

        except Exception as stream_err:
            # 只有 langgraph 的 GraphInterrupt 才算真正的 HITL
            err_type = type(stream_err).__name__
            if "GraphInterrupt" in err_type or "Interrupt" in err_type:
                log(f"⏸ HITL 自然中断: {stream_err}")
                hitl_triggered = True
                hitl_reason = str(stream_err)
            else:
                # 真正的错误直接抛出，不要伪装成 HITL
                log(f"❌ Agent 流执行异常: {err_type}: {stream_err}")
                import traceback
                log(traceback.format_exc()[:500])
                config.LOCAL_BACKUP_DIR = _original_backup_dir
                return 0

        # HITL 中断处理
        if hitl_triggered:
            while True:
                print()
                print(f"{'='*60}")
                print(f"  ⚠️ 触发 HITL 拦截: {hitl_reason}")
                print(f"  当前会话: {session_id}")
                print(f"  请输入 'resume' 继续执行，或 'abort' 终止任务")
                print(f"{'='*60}")

                if stop_event.is_set():
                    log("⏹ 外部停止信号")
                    break

                try:
                    user_input = input("  👤 > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    user_input = "abort"

                if user_input == "resume":
                    log("  ▶ 用户选择继续执行...")
                    hitl_triggered = False
                    try:
                        for event in app.stream(None, run_config):
                            if stop_event.is_set():
                                log("⏹ 工作流收到停止信号")
                                break
                            final_state = event
                    except Exception as resume_err:
                        log(f"⚠️ 恢复执行异常: {resume_err}")
                    break
                elif user_input == "abort":
                    log("  ⏹ 用户终止任务")
                    config.LOCAL_BACKUP_DIR = _original_backup_dir
                    return 0
                else:
                    print(f"  ❓ 无法识别指令: '{user_input}'，请输入 resume 或 abort")

        # 提取统计数据
        if final_state:
            # stream 返回的格式: {node_name: state_update}
            for node_name, node_state in final_state.items():
                if isinstance(node_state, dict):
                    stats = node_state.get("stats", {})
                    if stats.get("saved", 0) > 0:
                        saved_pages = stats["saved"]
                    # 检查 extracted_data
                    extracted = node_state.get("extracted_data", [])
                    if extracted:
                        log(f"\n  📊 已提取 {len(extracted)} 条文章数据")
                    # Token 用量
                    usage = node_state.get("token_usage", {})
                    if usage.get("prompt_tokens"):
                        log(f"  💰 Token 用量: Prompt={usage['prompt_tokens']}, Completion={usage['completion_tokens']}")

        # 从 memory 获取最终统计
        memory = UrlMemory()
        total_visited = memory.count_visited(base_url)

        # 打印汇总
        log(f"\n{'='*60}")
        log(f"  ✅ ReAct Agent 执行完成!")
        log(f"  长期记忆已访问 URL: {total_visited}")
        log(f"  已保存HTML: {saved_pages}")
        log(f"{'='*60}")

        config.LOCAL_BACKUP_DIR = _original_backup_dir
        return saved_pages

    except Exception as e:
        import traceback
        log(f"\n❌ ReAct Agent 异常: {e}")
        log(traceback.format_exc()[:800])
        config.LOCAL_BACKUP_DIR = _original_backup_dir
        return 0


def run_langgraph(target_url: str,
                  log_callback: Optional[Callable[[str], None]] = None,
                  stop_event: Optional[Event] = None,
                  site_dir: Optional[str] = None) -> int:
    """
    LangGraph BFS 模式入口（零 Token，不需要 API Key）。

    使用 bfs_app（BFS 全站爬取 + 面包屑多级目录），完全不调用 LLM。
    供 GUI 的"LangGraph 模式"和命令行直接使用。

    参数与 run_agent 相同。
    """
    log = log_callback or _default_log
    if stop_event is None:
        stop_event = Event()

    _original_backup_dir = config.LOCAL_BACKUP_DIR

    import nodes
    nodes._reset_csv_writer()

    url = target_url.strip()
    if not url: log("❌ 网址不能为空"); return 0
    if not url.startswith(("http://", "https://")): url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc: log(f"❌ 无法解析网址: {url}"); return 0
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    if site_dir: config.LOCAL_BACKUP_DIR = site_dir

    log(f"\n{'='*60}")
    log(f"  ⚡ LangGraph BFS 模式（零 Token，不需要 API Key）")
    log(f"  目标: {base_url} | 输出: {config.LOCAL_BACKUP_DIR}")
    log(f"{'='*60}")

    if stop_event.is_set():
        config.LOCAL_BACKUP_DIR = _original_backup_dir
        return 0

    from graph import bfs_app

    initial_state = {
        "root_url": url, "base_url": base_url,
        "max_depth": config.MAX_DEPTH, "max_nav_depth": config.MAX_NAV_DEPTH,
        "current_depth": 1, "visited": [],
        "url_queue": [{"url": url, "depth": 1, "nav_depth": 1, "breadcrumb": [], "parent_url": ""}],
        "current_page": {}, "results": [],
        "stats": {"total": 0, "success": 0, "failed": 0, "skipped": 0, "saved": 0},
        "error": "", "nav_mapping": {},
        "current_url": url, "extracted_data": [], "error_log": [],
        "token_usage": {}, "retry_count": 0, "fatal_error": False,
        "node_consecutive_failures": {},
        "messages": [], "task_complete": False,
        "hitl_interrupt": False, "hitl_reason": "",
    }

    try:
        final_state = None
        for event in bfs_app.stream(initial_state):
            if stop_event.is_set():
                log("⏹ 工作流收到停止信号"); break
            final_state = event

        saved_pages = 0
        if final_state:
            for node_state in final_state.values():
                if isinstance(node_state, dict):
                    saved_pages = node_state.get("stats", {}).get("saved", saved_pages) or saved_pages

        log(f"\n✅ BFS 模式完成 | 已保存: {saved_pages} 页")
        config.LOCAL_BACKUP_DIR = _original_backup_dir
        return saved_pages
    except Exception as e:
        import traceback
        log(f"❌ BFS 异常: {e}\n{traceback.format_exc()[:500]}")
        config.LOCAL_BACKUP_DIR = _original_backup_dir
        return 0


# ==========================================================================
# 多 Agent 流水线入口
# ==========================================================================

def run_multi_agent(target_url: str,
                    concurrency: int = 5,
                    log_callback: Optional[Callable[[str], None]] = None,
                    reset_memory: bool = False) -> int:
    """
    使用多 Agent 智能路由架构执行爬取。

    Args:
        target_url:    目标网站首页 URL
        concurrency:   抓取并发数
        log_callback:  日志回调
        reset_memory:  是否重置该站点的 SQLite 记忆

    Returns:
        成功保存的页面数
    """
    def log(msg: str) -> None:
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    # ── 预处理 URL ──
    from urllib.parse import urlparse, urlunparse, urljoin
    parsed = urlparse(target_url)
    if not parsed.scheme:
        target_url = "https://" + target_url
        parsed = urlparse(target_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    # ── 重置记忆 ──
    memory = UrlMemory()
    if reset_memory:
        deleted = memory.clear_site(base_url)
        log(f"🧹 已重置站点记忆: 清除 {deleted} 条记录")

    # ── 运行异步流水线 ──
    async def _run():
        pipeline = CrawlerPipeline(
            concurrency=concurrency,
            log_callback=log,
        )
        return await pipeline.run(target_url)

    try:
        stats = asyncio.run(_run())
        saved = stats.get("pages_saved", 0)
        fetched = stats.get("pages_fetched", 0)
        skipped = stats.get("pages_skipped", 0)
        failed = stats.get("pages_failed", 0)
        dups = stats.get("pages_duplicate", 0)
        total_links = stats.get("total_detail_links", 0)

        # ── 详细诊断 ──
        log(f"\n{'='*50}")
        log(f"📊 多 Agent 流水线诊断报告")
        log(f"  详情页链接数: {total_links}")
        log(f"  成功抓取:     {fetched}")
        log(f"  成功保存:     {saved}")
        log(f"  跳过:         {skipped}")
        log(f"  重复:         {dups}")
        log(f"  失败:         {failed}")
        if saved == 0:
            if total_links == 0:
                log(f"  ⚠️  根因: NavAgent 未提取到任何详情页链接")
                log(f"  💡 建议: ① 检查目标网站导航栏是否为 <nav>/<ul>/<li> 标准结构")
                log(f"           ② 如果网站是 SPA(React/Vue)，需先安装 Playwright: playwright install chromium")
            elif fetched == 0:
                log(f"  ⚠️  根因: FetcherRouter 未成功抓取任何页面")
                log(f"  💡 建议: ① 检查网络连接 ② 目标站点可能需要 VPN ③ 尝试安装 Playwright")
            elif saved == 0:
                log(f"  ⚠️  根因: ExtractorAgent 清洗后无有效内容（可能全是列表页/空内容）")
                log(f"  💡 建议: 检查目标站点详情页是否为 JS 动态渲染")
        log(f"{'='*50}")

        log(f"\n多 Agent 流水线完成 | 成功保存: {saved} 页")
        return saved

    except Exception as e:
        import traceback
        log(f"❌ 多 Agent 流水线异常: {e}\n{traceback.format_exc()[:500]}")
        return 0


# ============================================================================
# LangGraph 多 Agent 爬虫（新版：传统爬虫 + LLM 评估）
# ============================================================================

def run_langgraph_crawler(target_url: str,
                          concurrency: int = 5,
                          log_callback: Optional[Callable[[str], None]] = None,
                          reset_memory: bool = False) -> int:
    """
    使用 LangGraph StateGraph 架构执行多 Agent 爬取。

    架构特点:
      - 传统爬虫（httpx/Playwright + trafilatura/BS4）始终是默认执行者
      - LLM 仅在传统爬虫完成后评估结果质量
      - 如需调整，LLM 建议配置变更，最多 3 轮

    Args:
        target_url:    目标网站首页 URL
        concurrency:   保留参数（LangGraph 内部串行处理 queue）
        log_callback:  日志回调
        reset_memory:  是否重置该站点的 SQLite 记忆

    Returns:
        成功保存的页面数
    """
    def log(msg: str) -> None:
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    # ── 预处理 URL ──
    parsed = urlparse(target_url)
    if not parsed.scheme:
        target_url = "https://" + target_url
        parsed = urlparse(target_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    # ── 重置记忆 ──
    memory = UrlMemory()
    if reset_memory:
        deleted = memory.clear_site(base_url)
        log(f"🧹 已重置站点记忆: 清除 {deleted} 条记录")

    # ── 运行 LangGraph 工作流 ──
    try:
        stats = asyncio.run(
            run_langgraph_crawler_core(
                seed_url=target_url,
                log_callback=log,
            )
        )
        saved = stats.get("saved", 0)
        blocked_urls = stats.get("anti_crawl_blocked_urls", {})
        if blocked_urls:
            domain = parsed.netloc
            log(f"\n🛡️ [反爬拦截汇总] 共 {len(blocked_urls)} 个页面被反爬拦截（{domain}）:")
            for url_key, reason in list(blocked_urls.items())[:10]:
                log(f"  ⛔ {url_key[:80]} — 高级反爬爬不了")
                import logging
                logging.debug(f"[AntiCrawl Detail] URL={url_key}, 原因={reason}")
            if len(blocked_urls) > 10:
                log(f"  ... 还有 {len(blocked_urls) - 10} 个")
        # 兜底：如果 stats 不包含 saved，从磁盘统计 HTML 文件数
        # 实际输出目录由 scout_node 创建: output/<netloc>（保留 www），
        # 需同时兼容 output/<netloc> 与 output/<去掉 www 的 netloc> 两种历史目录。
        if saved == 0:
            domain = parsed.netloc.replace(":", "_")
            for cand in (os.path.join("output", domain),
                         os.path.join("output", domain.replace("www.", ""))):
                if os.path.isdir(cand):
                    cnt = sum(1 for dp, _, fs in os.walk(cand)
                              for f in fs if f.endswith('.html'))
                    if cnt > 0:
                        saved = cnt
                        log(f"  📁 (从磁盘恢复页数: {saved}) @ {cand}")
                        break
        log(f"\nLangGraph 爬虫完成 | 成功保存: {saved} 页")
        return saved
    except Exception as e:
        import traceback
        log(f"❌ LangGraph 爬虫异常: {e}\n{traceback.format_exc()[:500]}")
        return 0


if __name__ == "__main__":
    from schemas import agent_logger
    import argparse

    parser = argparse.ArgumentParser(description="企业级 AI Agent 爬虫")
    parser.add_argument("--reset", action="store_true", default=False,
                        help="重置站点记忆（清空 SQLite 中该站点的 URL 缓存和历史记录），白纸启动")
    parser.add_argument("--reset-all", action="store_true", default=False,
                        help="清空全部 SQLite 记忆（所有站点的 URL 缓存），全局重置")
    parser.add_argument("--multi-agent", action="store_true", default=False,
                        help="使用多 Agent 智能路由架构（Scout→Nav→Fetcher→Extractor→Storage）")
    parser.add_argument("--langgraph", action="store_true", default=False,
                        help="使用 LangGraph StateGraph 架构（传统爬虫 + LLM 评估）")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="多 Agent 模式并发数（默认5）")
    args = parser.parse_args()

    if args.reset_all:
        mem = UrlMemory()
        deleted = mem.clear_all()
        print(f"🧹 已清空全部记忆: {deleted} 条记录")
        if not input("继续输入网址: ").strip():
            sys.exit(0)

    agent_logger.info("Supervisor Agent 命令行模式启动 (Phase 3)")

    url = input("请输入公司网址: ").strip()
    if not url:
        print("网址不能为空")
        sys.exit(1)

    if args.multi_agent:
        print(f"启动多 Agent 智能路由架构 (并发={args.concurrency})")
        pages = run_multi_agent(url, concurrency=args.concurrency,
                                log_callback=print, reset_memory=args.reset or args.reset_all)
    elif args.langgraph:
        print(f"启动 LangGraph 多 Agent 架构 (传统爬虫 + LLM 评估)")
        pages = run_langgraph_crawler(url, concurrency=args.concurrency,
                                       log_callback=print, reset_memory=args.reset or args.reset_all)
    else:
        pages = run_agent(url, log_callback=print, reset_memory=args.reset or args.reset_all)

    print(f"\n最终保存页面数: {pages}")
    agent_logger.info(f"任务结束 | saved_pages={pages}")
