"""
分布式任务队列（stdlib sqlite3，零新依赖）— 阶段3 轻量分布式爬虫调度。

⚠️ 命名说明：本模块叫 task_queue 而非 queue——python distributed/scheduler.py
启动时脚本目录 distributed/ 会进入 sys.path[0]，若命名 queue.py 会遮蔽标准库
queue（LangGraph 内部依赖 queue.LifoQueue），运行期直接 AttributeError。

面试叙事定位：
  - 为什么不上 Redis/Celery：任务量是"几十个站点"，瓶颈在站点抓取 IO 不在队列
    吞吐；内网无 Redis；SQLite 写锁 + BEGIN IMMEDIATE 事务足够支撑 N 个 worker
    的 claim 互斥（单连接单写者语义），零运维成本。
  - 租约（lease）模型：worker 崩溃后任务靠 lease_until 超时被 requeue_stale 回收，
    不会永远卡在 running——这是"至少一次"语义的轻量实现。
  - 跨进程 URL 去重不在此层：由共享 agent_memory.db 的 visited_urls 唯一约束保证
    （多 worker 各自进程内单例，指向同一 SQLite 文件）。

用法（scheduler 子命令封装）：
  q = TaskQueue("crawl_tasks.db")
  task_id = q.enqueue("https://a.com/")
  task = q.claim("worker-1")            # 原子抢占，另一 worker 拿不到同一任务
  q.heartbeat("worker-1")               # 长任务定期续租
  q.complete(task["id"], saved=86)      # 或 q.fail(task["id"], "超时")
  n = q.requeue_stale(lease_seconds=300)
"""
import sqlite3
import time
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site_url     TEXT    NOT NULL,
    concurrency  INTEGER NOT NULL DEFAULT 5,
    reset_memory INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
    priority     INTEGER NOT NULL DEFAULT 0,
    worker       TEXT,
    lease_until  REAL,                                -- 租约到期 epoch 秒
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    saved        INTEGER,
    created_at   TEXT    NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class TaskQueue:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        with sqlite3.connect(db_path, timeout=15) as conn:
            conn.execute(SCHEMA)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, timeout=15)

    # ── 写操作：入队 / 状态流转 ──

    def enqueue(self, site_url: str, concurrency: int = 5,
                reset_memory: bool = False, priority: int = 0) -> int:
        """入队一个站点任务，返回任务 id。"""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO crawl_tasks (site_url, concurrency, reset_memory, "
                "priority, created_at) VALUES (?, ?, ?, ?, ?)",
                (site_url, int(concurrency), int(reset_memory), int(priority), _now()))
            conn.commit()
            return int(cur.lastrowid)

    def claim(self, worker_id: str, lease_seconds: float = 300.0) -> Optional[Dict[str, Any]]:
        """原子抢占一条 pending 任务（BEGIN IMMEDIATE 写锁防双 worker 同抢）。"""
        now = time.time()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, site_url, concurrency, reset_memory FROM crawl_tasks "
                "WHERE status='pending' ORDER BY priority DESC, id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE crawl_tasks SET status='running', worker=?, lease_until=?, "
                "attempts=attempts+1, started_at=?, error=NULL WHERE id=?",
                (worker_id, now + lease_seconds, _now(), row[0]))
            conn.commit()
            return {"id": row[0], "site_url": row[1],
                    "concurrency": row[2], "reset_memory": bool(row[3])}

    def heartbeat(self, worker_id: str, lease_seconds: float = 300.0) -> int:
        """续租该 worker 的 running 任务，返回续租任务数。"""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE crawl_tasks SET lease_until=? "
                "WHERE status='running' AND worker=?",
                (time.time() + lease_seconds, worker_id))
            conn.commit()
            return int(cur.rowcount)

    def complete(self, task_id: int, saved: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE crawl_tasks SET status='done', saved=?, finished_at=? WHERE id=?",
                (int(saved), _now(), task_id))
            conn.commit()

    def fail(self, task_id: int, error: str, max_attempts: int = 2) -> str:
        """失败：未超重试上限 → 回 pending（attempts 已+1）；否则终态 failed。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT attempts FROM crawl_tasks WHERE id=?", (task_id,)).fetchone()
            attempts = int(row[0]) if row else 0
            if attempts >= max_attempts:
                conn.execute(
                    "UPDATE crawl_tasks SET status='failed', error=?, worker=NULL, "
                    "lease_until=NULL, finished_at=? WHERE id=?",
                    (str(error)[:500], _now(), task_id))
                outcome = "failed"
            else:
                conn.execute(
                    "UPDATE crawl_tasks SET status='pending', error=?, worker=NULL, "
                    "lease_until=NULL WHERE id=?",
                    (str(error)[:500], task_id))
                outcome = "retry"
            conn.commit()
            return outcome

    def requeue_stale(self, lease_seconds: float = 300.0) -> int:
        """回收租约过期的 running 任务（worker 崩溃场景）→ 回 pending。"""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE crawl_tasks SET status='pending', worker=NULL, lease_until=NULL "
                "WHERE status='running' AND lease_until < ?",
                (time.time(),))
            conn.commit()
            return int(cur.rowcount)

    # ── 读操作 ──

    def stats(self) -> Dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM crawl_tasks GROUP BY status").fetchall()
            return {status: int(n) for status, n in rows}

    def list_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM crawl_tasks ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
            return [dict(r) for r in rows]

    def reset(self) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM crawl_tasks")
            conn.commit()
