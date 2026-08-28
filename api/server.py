"""
FastAPI 服务化 — 把多 Agent 爬虫包成 REST 服务（提交任务 / 查进度 / 取结果）。

面试叙事定位：
  - 服务化与 SLO 叙事的最小闭环：后台任务隔离（爬虫同步入口经 asyncio.to_thread
    卸载到线程池，不阻塞事件循环）、全局单爬虫槽（SQLite 记忆与输出目录竞争，
    第二个提交 409）、API key 鉴权、提交频率限流、日志环形缓冲可观测；
  - 与 GUI 桌面壳共用同一入口 run_langgraph_crawler——服务层零业务逻辑。

接口：
  POST /crawl                 提交爬取任务 → 202 {task_id}（槽忙 → 409）
  GET  /tasks/{task_id}       状态/进度/日志尾部 → running|done|failed
  GET  /tasks/{task_id}/results  落盘 CSV 行（任务完成前 → 409）

鉴权：请求头 X-API-Key；环境变量 CRAWLER_API_KEY 未设置时放行（本地开发友好）。
限流：单爬虫槽 + 每客户端 60s 滑动窗口内最多 6 次提交（429）。

运行：
  uvicorn api.server:app --host 0.0.0.0 --port 8000
  # 或 python api/server.py
"""
import asyncio
import csv
import os
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(
    title="Crawler Agent API",
    version="1.0.0",
    description="多 Agent 网页采集服务（Supervisor 编排 + LLM 熔断/批量抢救）",
)

# ── 运行时状态（进程内；单进程部署，多实例需外部协调——见 README trade-off） ──
_TASKS: Dict[str, Dict[str, Any]] = {}
_TASK_ORDER: deque = deque(maxlen=20)        # 任务表上限（防内存涨，旧的淘汰）
_LOG_RING: int = 4000                        # 每任务日志环形缓冲上限
_SUBMIT_WINDOW: Dict[str, deque] = {}        # client -> 提交时间戳窗口（限流）
_MAX_SUBMITS_PER_WINDOW = 6
_WINDOW_SECONDS = 60.0


class CrawlRequest(BaseModel):
    url: str = Field(..., description="目标网站首页 URL")
    concurrency: int = Field(5, ge=1, le=20)
    reset_memory: bool = Field(False, description="清空该站点记忆与旧输出后重爬")


def _client_key(x_api_key: Optional[str], x_forwarded: Optional[str]) -> str:
    """限流主体：优先 X-Forwarded-For（反代后真实 IP），退回 API key / 'anon'。"""
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return x_api_key or "anon"


def _check_submit_rate(client: str) -> None:
    now = time.monotonic()
    win = _SUBMIT_WINDOW.setdefault(client, deque())
    while win and now - win[0] > _WINDOW_SECONDS:
        win.popleft()
    if len(win) >= _MAX_SUBMITS_PER_WINDOW:
        retry = int(_WINDOW_SECONDS - (now - win[0])) + 1
        raise HTTPException(429, f"提交过于频繁，{retry}s 后重试")
    win.append(now)


def _check_api_key(x_api_key: Optional[str]) -> None:
    """鉴权：CRAWLER_API_KEY 未配置 → 放行；配置 → 必须匹配。"""
    expected = os.environ.get("CRAWLER_API_KEY", "")
    if expected and x_api_key != expected:
        raise HTTPException(401, "无效或缺失的 X-API-Key")


def _running_task() -> Optional[Dict[str, Any]]:
    return next((t for t in _TASKS.values() if t["status"] == "running"), None)


def _prune_tasks() -> None:
    while len(_TASK_ORDER) > _TASK_ORDER.maxlen:
        _TASKS.pop(_TASK_ORDER.popleft(), None)


async def _run_crawl(task: Dict[str, Any]) -> None:
    """后台任务：同步爬虫入口卸载到线程池执行，回调回填日志与进度。"""
    from main import run_langgraph_crawler

    def _log(msg: str) -> None:
        task["logs"].append(str(msg))

    def _progress(fetched: int, queue_len: int, url: str, phase: str) -> None:
        task["progress"] = {"fetched": fetched, "queue_len": queue_len,
                            "url": url, "phase": phase}

    try:
        saved = await asyncio.to_thread(
            run_langgraph_crawler, task["url"], concurrency=task["concurrency"],
            log_callback=_log, reset_memory=task["reset_memory"],
            progress_callback=_progress,
        )
        task["saved"] = int(saved or 0)
        task["status"] = "done"
    except Exception as e:  # noqa: BLE001 — 任务失败必须回填可诊断信息
        task["status"] = "failed"
        task["error"] = f"{type(e).__name__}: {e}"
        task["logs"].append(f"[api] 任务失败: {task['error']}")
    finally:
        task["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def _find_csv(url: str) -> Optional[Path]:
    """定位该站点最近一次落盘的 CSV（output/{domain}/crawl_results.csv）。"""
    from urllib.parse import urlparse

    netloc = urlparse(url if "://" in url else "https://" + url).netloc
    for domain in filter(None, {netloc, netloc.replace("www.", "")}):
        p = PROJECT_ROOT / "output" / domain / "crawl_results.csv"
        if p.is_file():
            return p
    return None


@app.post("/crawl", status_code=202)
async def submit_crawl(
    req: CrawlRequest,
    x_api_key: Optional[str] = Header(None),
    x_forwarded_for: Optional[str] = Header(None),
):
    _check_api_key(x_api_key)
    _check_submit_rate(_client_key(x_api_key, x_forwarded_for))
    busy = _running_task()
    if busy:
        return JSONResponse(status_code=409, content={
            "detail": "已有爬取任务在运行（单爬虫槽）", "running_task_id": busy["task_id"]})
    task_id = uuid.uuid4().hex[:12]
    task = {
        "task_id": task_id, "url": req.url, "concurrency": req.concurrency,
        "reset_memory": req.reset_memory, "status": "running",
        "saved": None, "error": None, "progress": None,
        "logs": deque(maxlen=_LOG_RING),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "finished_at": None,
    }
    _TASKS[task_id] = task
    _TASK_ORDER.append(task_id)
    _prune_tasks()
    asyncio.get_running_loop()  # 显式断言事件循环内
    task["future"] = asyncio.create_task(_run_crawl(task))
    return {"task_id": task_id, "status": "running", "poll": f"/tasks/{task_id}"}


@app.get("/tasks/{task_id}")
async def task_status(task_id: str, x_api_key: Optional[str] = Header(None)):
    _check_api_key(x_api_key)
    task = _TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在或已被淘汰")
    logs = list(task["logs"])
    return {
        "task_id": task_id, "url": task["url"], "status": task["status"],
        "saved": task["saved"], "error": task["error"], "progress": task["progress"],
        "started_at": task["started_at"], "finished_at": task["finished_at"],
        "logs_tail": logs[-50:],
    }


@app.get("/tasks/{task_id}/results")
async def task_results(task_id: str, limit: int = 200, x_api_key: Optional[str] = Header(None)):
    _check_api_key(x_api_key)
    task = _TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在或已被淘汰")
    if task["status"] == "running":
        return JSONResponse(status_code=409, content={"detail": "任务仍在运行，完成后可取结果"})
    csv_path = _find_csv(task["url"])
    if not csv_path:
        raise HTTPException(404, f"未找到结果 CSV（url={task['url']}）")
    limit = max(1, min(limit, 1000))
    rows: List[Dict[str, str]] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if i >= limit:
                break
            # html 列是完整清洗后源码，列表场景太大 → 截断预览
            if row.get("html"):
                row["html"] = row["html"][:300] + ("..." if len(row["html"]) > 300 else "")
            rows.append(row)
    return {"task_id": task_id, "csv_path": str(csv_path),
            "returned": len(rows), "rows": rows}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
