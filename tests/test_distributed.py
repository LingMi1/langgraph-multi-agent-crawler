"""分布式队列与多进程 worker 单测（stdlib sqlite3，零新依赖）。"""
import multiprocessing
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from distributed.task_queue import TaskQueue
from distributed.scheduler import worker_loop


@pytest.fixture()
def q(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    yield queue
    queue.reset()


# ── 队列状态机 ──

def test_enqueue_claim_complete_flow(q):
    tid = q.enqueue("http://a.com/")
    task = q.claim("w1")
    assert task["id"] == tid
    assert task["site_url"] == "http://a.com/"
    assert task["concurrency"] == 5
    assert task["reset_memory"] is False
    q.complete(tid, saved=86)
    assert q.stats() == {"done": 1}


def test_claim_never_double_assigns_same_task(q):
    ids = {q.enqueue(f"http://s{i}.com/") for i in range(3)}
    claimed = {q.claim("w1")["id"] for _ in range(3)}
    assert claimed == ids  # 三个不同任务各被拿一次


def test_claim_empty_returns_none(q):
    assert q.claim("w1") is None


def test_priority_ordering(q):
    low = q.enqueue("http://low.com/", priority=0)
    high = q.enqueue("http://high.com/", priority=5)
    assert q.claim("w1")["id"] == high
    assert q.claim("w1")["id"] == low


# ── 租约与崩溃回收 ──

def test_heartbeat_renews_lease(q):
    tid = q.enqueue("http://slow.com/")
    q.claim("w1", lease_seconds=0.3)
    time.sleep(0.2)
    q.heartbeat("w1", lease_seconds=0.5)  # 续租：deadline 从当前时刻重新起算
    time.sleep(0.2)  # 总流逝 0.4s，但续租后 deadline 更晚（余量 0.3s，规避调度抖动）
    assert q.requeue_stale(lease_seconds=0.5) == 0  # 心跳续过，未过期
    assert q.stats() == {"running": 1}


def test_requeue_stale_recovers_crashed_worker(q):
    tid = q.enqueue("http://crash.com/")
    q.claim("w1", lease_seconds=0.2)
    time.sleep(0.3)  # 无心跳 → 租约过期（模拟 worker 被杀）
    assert q.requeue_stale(lease_seconds=0.2) == 1
    assert q.stats() == {"pending": 1}
    # 其他 worker 能重新领到该任务
    assert q.claim("w2")["id"] == tid


# ── 失败重试与终态 ──

def test_fail_retries_then_terminal(q):
    tid = q.enqueue("http://retry.com/")
    q.claim("w1")
    assert q.fail(tid, "boom", max_attempts=2) == "retry"  # attempts=1 < 2
    assert q.stats() == {"pending": 1}
    q.claim("w1")
    assert q.fail(tid, "boom", max_attempts=2) == "failed"  # attempts=2 >= 2
    assert q.stats() == {"failed": 1}


def test_enqueue_reset_memory_flag(q):
    tid = q.enqueue("http://r.com/", reset_memory=True, concurrency=3)
    task = q.claim("w1")
    assert task["reset_memory"] is True
    assert task["concurrency"] == 3


# ── 多进程消费：恰好一次语义 ──

def _fast_executor(task):
    """假执行器：模块级（spawn 可 pickle），返回保存页数。"""
    time.sleep(0.03)
    return len(task["site_url"])


def test_multiprocess_workers_consume_once(tmp_path):
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    for i in range(8):
        queue.enqueue(f"http://site{i}.com/", concurrency=2)

    stop = multiprocessing.Event()
    procs = [multiprocessing.Process(
        target=worker_loop,
        args=(queue, f"w{i}", stop, 60.0, _fast_executor),
        daemon=True) for i in range(2)]
    for p in procs:
        p.start()

    deadline = time.time() + 20
    while time.time() < deadline:
        stats = queue.stats()
        if stats.get("done", 0) == 8:
            break
        time.sleep(0.1)
    stop.set()
    for p in procs:
        p.join(timeout=5)

    assert queue.stats() == {"done": 8}
    # 恰好一次：每个任务只被 claim 一次（attempts==1），无双执行
    for t in queue.list_tasks(100):
        assert t["status"] == "done"
        assert t["attempts"] == 1
        assert t["saved"] == len(t["site_url"])
