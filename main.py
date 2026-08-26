"""
============================================================================
  企业级 AI Agent 爬取工作流 — LangGraph MA 模式
  基于 LangGraph StateGraph 的多 Agent 爬虫：
    传统爬虫（httpx/Playwright + trafilatura/BS4）始终是默认执行者
    → LLM 仅在传统爬虫完成后评估结果质量（最多 3 轮自动调整）

  用法:
    python main.py                         # 命令行交互模式
    from main import run_langgraph_crawler # 供 GUI 调用
============================================================================
"""

import sys
import os
import asyncio
from typing import Callable, Optional
from urllib.parse import urlparse

from schemas import agent_logger
from memory import UrlMemory

# LangGraph 多 Agent 爬虫（StateGraph 工作流）
from graph.workflow import run_crawler as run_langgraph_crawler_core


# ============================================================================
# LangGraph 多 Agent 爬虫（传统爬虫 + LLM 评估）
# ============================================================================

def run_langgraph_crawler(target_url: str,
                          concurrency: int = 5,
                          log_callback: Optional[Callable[[str], None]] = None,
                          reset_memory: bool = False,
                          progress_callback: Optional[Callable[[int, int, str], None]] = None) -> int:
    """
    使用 LangGraph StateGraph 架构执行多 Agent 爬取。

    架构特点:
      - 传统爬虫（httpx/Playwright + trafilatura/BS4）始终是默认执行者
      - LLM 仅在传统爬虫完成后评估结果质量
      - 如需调整，LLM 建议配置变更，最多 3 轮

    Args:
        target_url:    目标网站首页 URL
        concurrency:   fetch_extract 节点每批并发处理的 URL 数（默认 5）
        log_callback:  日志回调
        reset_memory:  是否重置该站点的 SQLite 记忆
        progress_callback: 进度回调 (fetched, queue_len, url, phase)

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
        # ★ 同步清理上次运行的输出残留（巨型 base64 列表页/重复 _N 文件等），
        #   避免新运行与旧文件撞名产生 _N.html 累积（配合 _save_html_file 覆盖逻辑）
        import shutil
        domain = parsed.netloc.replace(":", "_")
        for cand in (os.path.join("output", domain),
                     os.path.join("output", domain.replace("www.", ""))):
            if os.path.isdir(cand):
                shutil.rmtree(cand, ignore_errors=True)
                log(f"🧹 已清理旧输出目录: {cand}")

    # ── 运行 LangGraph 工作流 ──
    try:
        stats = asyncio.run(
            run_langgraph_crawler_core(
                seed_url=target_url,
                log_callback=log,
                progress_callback=progress_callback,
                concurrency=concurrency,
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
    import argparse

    parser = argparse.ArgumentParser(description="企业级 AI Agent 爬虫（LangGraph MA 模式）")
    parser.add_argument("--reset", action="store_true", default=False,
                        help="重置站点记忆（清空 SQLite 中该站点的 URL 缓存和历史记录），白纸启动")
    parser.add_argument("--reset-all", action="store_true", default=False,
                        help="清空全部 SQLite 记忆（所有站点的 URL 缓存），全局重置")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="fetch_extract 节点并发数（默认5）")
    args = parser.parse_args()

    if args.reset_all:
        mem = UrlMemory()
        deleted = mem.clear_all()
        print(f"🧹 已清空全部记忆: {deleted} 条记录")
        if not input("继续输入网址: ").strip():
            sys.exit(0)

    agent_logger.info("LangGraph MA 命令行模式启动")

    url = input("请输入公司网址: ").strip()
    if not url:
        print("网址不能为空")
        sys.exit(1)

    pages = run_langgraph_crawler(url, concurrency=args.concurrency,
                                  log_callback=print, reset_memory=args.reset or args.reset_all)

    print(f"\n最终保存页面数: {pages}")
    agent_logger.info(f"任务结束 | saved_pages={pages}")
