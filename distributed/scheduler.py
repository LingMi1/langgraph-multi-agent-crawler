"""
轻量分布式调度器 — 多进程 worker 消费 SQLite 任务队列，整站级并行。

架构（面试叙事）：
  - 调度维度 = 站点：每个 worker 进程跑一个站点的完整 LangGraph workflow
    （run 内仍是 asyncio 并发），避免跨进程共享 BFS 队列/state 的架构破坏；
  - URL 跨进程去重由共享 agent_memory.db 的 visited_urls 唯一约束保证（零新增代码）；
  - 崩溃自愈：worker 异常 → fail() 回队重试（attempts 上限后终态 failed）；
    进程被杀 → 租约过期 → requeue_stale() 回收；
  - 为什么不用 Redis/Celery：任务量是几十个站点、瓶颈在站点 IO，SQLite 写锁 +
    BEGIN IMMEDIATE 足够，零运维（内网约束）。

用法：
  python distributed/scheduler.py enqueue urls.txt [--workers 3]
  python distributed/scheduler.py run-workers --workers 2 --lease 300
  python distributed/scheduler.py status
"""
import argparse
import multiprocessing
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from distributed.task_queue import TaskQueue  # noqa: E402

DEFAULT_DB = str(PROJECT_ROOT / "crawl_tasks.db")


# ── worker 执行器（可 pickle，multiprocessing spawn 可传） ──

def run_site_task(task: Dict[str, Any]) -> int:
    """在 worker 进程内执行一个站点任务，返回保存页数（进程隔离的完整 workflow）。"""
    from main import run_langgraph_crawler

    def _log(msg: str) -> None:
        print(f"  [worker:{task['id']}] {msg}", flush=True)

    return run_langgraph_crawler(
        target_url=task["site_url"],
        concurrency=task["concurrency"],
        log_callback=_log,
        reset_memory=task["reset_memory"],
    )


def _heartbeat_loop(queue: TaskQueue, worker_id: str, lease_seconds: float,
                    stop: threading.Event) -> None:
    """后台线程定期续租，防止长任务租约过期被 requeue_stale 误回收。"""
    while not stop.is_set():
        queue.heartbeat(worker_id, lease_seconds)
        stop.wait(lease_seconds / 3.0)


def worker_loop(queue: TaskQueue, worker_id: str, stop: Any,
                lease_seconds: float = 300.0,
                executor: Callable[[Dict[str, Any]], int] = run_site_task,
                requeue_stale_interval: float = 10.0) -> None:
    """worker 主循环：抢占 → 心跳线程 → 执行 → 状态流转 → 空闲回收过期租约。"""
    print(f"[worker] {worker_id} 启动", flush=True)
    stop_thread = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop, args=(queue, worker_id, lease_seconds, stop_thread),
        daemon=True)
    heartbeat.start()
    last_stale = time.monotonic()
    try:
        while not stop.is_set():
            task = queue.claim(worker_id, lease_seconds)
            if task is None:
                # 空闲巡检：顺带回收其他崩溃 worker 的过期租约
                if time.monotonic() - last_stale >= requeue_stale_interval:
                    n = queue.requeue_stale(lease_seconds)
                    if n:
                        print(f"[worker] {worker_id} 回收 {n} 个过期租约", flush=True)
                    last_stale = time.monotonic()
                stop.wait(1.0)
                continue
            print(f"[worker] {worker_id} 领取任务 #{task['id']} "
                  f"{task['site_url'][:60]}", flush=True)
            try:
                saved = executor(task)
                queue.complete(task["id"], int(saved or 0))
                print(f"[worker] {worker_id} 任务 #{task['id']} 完成 saved={saved}", flush=True)
            except Exception as e:  # noqa: BLE001 — worker 异常必须回队重试
                outcome = queue.fail(task["id"], f"{type(e).__name__}: {e}")
                print(f"[worker] {worker_id} 任务 #{task['id']} 失败→{outcome}: {e}",
                      flush=True)
    finally:
        stop_thread.set()


# ── 子命令实现 ──

def _cmd_enqueue(args: argparse.Namespace) -> int:
    queue = TaskQueue(args.db)
    urls = [u.strip() for u in args.urls_file.read_text(encoding="utf-8").splitlines()
            if u.strip()]
    ids = [queue.enqueue(u, concurrency=args.concurrency, reset_memory=args.reset)
           for u in urls]
    print(f"已入队 {len(ids)} 个站点任务: {ids}")
    return 0


def _cmd_run_workers(args: argparse.Namespace) -> int:
    queue = TaskQueue(args.db)
    stop = multiprocessing.Event()
    procs = []
    for i in range(args.workers):
        p = multiprocessing.Process(
            target=worker_loop,
            args=(queue, f"worker-{i + 1}", stop, args.lease),
            name=f"crawler-worker-{i + 1}",
            daemon=True)
        p.start()
        procs.append(p)
    print(f"调度器已启动 {args.workers} 个 worker（Ctrl+C 优雅退出）", flush=True)
    try:
        while True:
            time.sleep(2)
            stats = queue.stats()
            if not any(p.is_alive() for p in procs):
                break
            if stats.get("pending", 0) == 0 and stats.get("running", 0) == 0:
                # 队列空且无运行中任务：给 worker 一个轮询窗口，仍空则收工
                time.sleep(args.drain)
                stats = queue.stats()
                if stats.get("pending", 0) == 0 and stats.get("running", 0) == 0:
                    print("队列已空，worker 空闲，调度器收工", flush=True)
                    break
    except KeyboardInterrupt:
        print("\n收到中断，正在优雅退出…", flush=True)
    finally:
        stop.set()
        for p in procs:
            p.join(timeout=10)
            if p.is_alive():
                p.terminate()
        print(f"最终队列状态: {queue.stats()}", flush=True)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    queue = TaskQueue(args.db)
    print(f"队列统计: {queue.stats()}")
    print("-" * 60)
    for t in queue.list_tasks(args.limit):
        print(f"#{t['id']:<4} {t['status']:<8} worker={str(t['worker']):<10} "
              f"attempts={t['attempts']} saved={t['saved']} "
              f"err={str(t['error'])[:40] or '-'} | {t['site_url'][:50]}")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="scheduler", description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help="任务队列 SQLite 路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_enq = sub.add_parser("enqueue", help="批量入队站点任务")
    p_enq.add_argument("urls_file", type=Path, help="站点 URL 列表文件（每行一个）")
    p_enq.add_argument("--concurrency", type=int, default=5)
    p_enq.add_argument("--reset", action="store_true", help="重爬时清记忆")
    p_enq.set_defaults(fn=_cmd_enqueue)

    p_run = sub.add_parser("run-workers", help="启动 N 个 worker 消费队列")
    p_run.add_argument("--workers", type=int, default=2)
    p_run.add_argument("--lease", type=float, default=300.0, help="任务租约秒数")
    p_run.add_argument("--drain", type=float, default=3.0,
                       help="队列空后收工前的空转窗口秒数")
    p_run.set_defaults(fn=_cmd_run_workers)

    p_st = sub.add_parser("status", help="查看队列状态")
    p_st.add_argument("--limit", type=int, default=50)
    p_st.set_defaults(fn=_cmd_status)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
