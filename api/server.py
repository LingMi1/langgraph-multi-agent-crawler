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
  POST /chat/stream           SSE 流式调用 LLM（多 provider 故障转移 + 熔断）

鉴权：请求头 X-API-Key；环境变量 CRAWLER_API_KEY 未设置时放行（本地开发友好）。
限流：单爬虫槽 + 每客户端 60s 滑动窗口内最多 6 次提交（429）。

运行：
  uvicorn api.server:app --host 0.0.0.0 --port 8000
  # 或 python api/server.py
"""
import asyncio
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
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
_STREAM_WINDOW: Dict[str, deque] = {}        # client -> SSE 连接时间戳窗口（防滥用）
_MAX_STREAMS_PER_WINDOW = 30
_WINDOW_SECONDS = 60.0


class CrawlRequest(BaseModel):
    url: str = Field(..., description="目标网站首页 URL")
    concurrency: int = Field(5, ge=1, le=20)
    reset_memory: bool = Field(False, description="清空该站点记忆与旧输出后重爬")


class ChatStreamRequest(BaseModel):
    """/chat/stream SSE 请求体。"""
    system: str = Field("", description="system prompt")
    prompt: str = Field(..., description="user prompt")
    temperature: float = Field(0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(32768, ge=1, le=131072)


def _client_key(x_api_key: Optional[str], x_forwarded: Optional[str]) -> str:
    """限流主体：优先 X-Forwarded-For（反代后真实 IP），退回 API key / 'anon'。"""
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return x_api_key or "anon"


def _check_sliding_rate(window: Dict[str, deque], client: str, limit: int, label: str) -> None:
    """滑动窗口限流：limit 次/窗口秒，超限 429。submit 与 SSE 两条入口共用。"""
    now = time.monotonic()
    win = window.setdefault(client, deque())
    while win and now - win[0] > _WINDOW_SECONDS:
        win.popleft()
    if len(win) >= limit:
        retry = int(_WINDOW_SECONDS - (now - win[0])) + 1
        raise HTTPException(429, f"{label}，{retry}s 后重试")
    win.append(now)


def _check_submit_rate(client: str) -> None:
    _check_sliding_rate(_SUBMIT_WINDOW, client, _MAX_SUBMITS_PER_WINDOW, "提交过于频繁")


def _check_stream_rate(client: str) -> None:
    _check_sliding_rate(_STREAM_WINDOW, client, _MAX_STREAMS_PER_WINDOW, "SSE 连接过于频繁")


def _check_api_key(x_api_key: Optional[str]) -> None:
    """鉴权：CRAWLER_API_KEY 未配置 → 放行；配置 → 必须匹配（恒定时间比较防时序侧信道）。"""
    expected = os.environ.get("CRAWLER_API_KEY", "")
    if expected and (x_api_key is None or not secrets.compare_digest(x_api_key, expected)):
        raise HTTPException(401, "无效或缺失的 X-API-Key")


def _extract_key(x_api_key: Optional[str], authorization: Optional[str]) -> Optional[str]:
    """兼容两种鉴权头：X-API-Key（旧接口）或 Authorization: Bearer <key>（工作台前端）。"""
    if x_api_key:
        return x_api_key
    if authorization:
        for prefix in ("Bearer ", "bearer "):
            if authorization.startswith(prefix):
                return authorization[len(prefix):].strip()
    return None


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


@app.post("/crawl", status_code=202, response_model=None)
async def submit_crawl(
    req: CrawlRequest,
    x_api_key: Optional[str] = Header(None),
    x_forwarded_for: Optional[str] = Header(None),
) -> Union[Dict[str, Any], JSONResponse]:
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


@app.get("/tasks/{task_id}", response_model=None)
async def task_status(task_id: str, x_api_key: Optional[str] = Header(None)) -> Union[Dict[str, Any], JSONResponse]:
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


@app.get("/tasks/{task_id}/results", response_model=None)
async def task_results(task_id: str, limit: int = 200, x_api_key: Optional[str] = Header(None)) -> Union[Dict[str, Any], JSONResponse]:
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


@app.post("/chat/stream", response_model=None)
async def chat_stream_endpoint(
    req: ChatStreamRequest,
    x_api_key: Optional[str] = Header(None),
    x_forwarded_for: Optional[str] = Header(None),
) -> StreamingResponse:
    """SSE 流式调用 LLM（text/event-stream）。

    故障转移/熔断/多 provider 全在 agents.llm_pipeline.chat_stream 内部完成，
    这里只负责鉴权、限流与把产出片段包成 SSE 帧；prompt 为空或失败时只发 [DONE]。
    """
    _check_api_key(x_api_key)
    _check_stream_rate(_client_key(x_api_key, x_forwarded_for))
    from agents.llm_pipeline import chat_stream

    async def _sse() -> AsyncIterator[str]:
        if not req.prompt.strip():
            # 空 prompt：无意义的 LLM 调用，直接短路（零成本）
            yield "data: [DONE]\n\n"
            return
        async for piece in chat_stream(
            req.system, req.prompt,
            temperature=req.temperature, max_tokens=req.max_tokens,
        ):
            yield f"data: {piece}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_sse(), media_type="text/event-stream")


# ════════════════════════════════════════════════════════════════════════════
#  博宇工作台照搬适配层：静态托管 + LLM 配置中心（config.json 只读收敛）
#  + 结果浏览/导出/入库 6 接口 + /orchestrator/stream SSE 适配（8 phase → 6 节点）
# ════════════════════════════════════════════════════════════════════════════

_WORKBENCH_DIR = Path(__file__).resolve().parent / "static" / "workbench"
_CONFIG_PATH = PROJECT_ROOT / "config.json"
_OUTPUT_DIR = PROJECT_ROOT / "output"

# 8 phase → 6 DAG 节点映射（scout/navigate/fetch/rescue_locate/rescue/evaluate/media/storage）
_NODE_BY_PHASE = {
    "scout": "probe", "navigate": "derive", "fetch": "crawl",
    "rescue_locate": "crawl", "rescue": "crawl",
    "evaluate": "validate", "media": "clean", "storage": "finalize",
}
_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9.\-:]+$")   # 域名（含端口）防路径穿越
_FILEID_RE = re.compile(r"^[0-9a-f]{16}\.html$")  # 文件名=url 的 md5 前 16 位


class OrchestratorStreamRequest(BaseModel):
    """工作台 /orchestrator/stream 请求体（博宇前端契约）。"""
    url: str = Field(..., description="目标网站首页 URL")
    provider: str = Field("deepseek")
    model: str = Field("")
    api_key_env: str = Field("")
    max_depth: int = Field(3)
    max_pages: int = Field(50)
    concurrent: int = Field(3, ge=1, le=20)
    delay: float = Field(0.2)
    timeout: int = Field(60)
    output: str = Field("crawl_output")
    no_cache: bool = Field(False)
    enable_cleaning: bool = Field(True)
    cleaning_mode: str = Field("standard")
    keep_images: bool = Field(True)
    cleaning_batch_size: int = Field(20)
    site_mode: str = Field("auto")


class LlmConfigRequest(BaseModel):
    active_provider: str = Field("")
    active_model: str = Field("")


class LlmKeysRequest(BaseModel):
    keys: Dict[str, str] = Field(default_factory=dict)


class LlmKeyTestRequest(BaseModel):
    provider: str = Field("")
    api_key: str = Field("")
    model: str = Field("")


class SaveContentRequest(BaseModel):
    content: str = Field("")


# ── config.json 读写（LLM 配置中心唯一数据源） ──
def _load_config() -> Dict[str, Any]:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: Dict[str, Any]) -> None:
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ── 域名 / 结果目录工具 ──
def _domain_of(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else "https://" + url)
    return parsed.netloc or ""


def _file_id(url: str) -> str:
    """url → 稳定文件名（md5 前 16 位）。result/cleaned/save 接口据此反查 CSV 行。"""
    return hashlib.md5((url or "").encode("utf-8")).hexdigest()[:16] + ".html"


def _domain_dir(domain: str) -> Optional[Path]:
    if not domain or not _DOMAIN_RE.match(domain):
        return None
    p = _OUTPUT_DIR / domain
    return p if p.is_dir() else None


def _read_csv_rows(domain: str) -> List[Dict[str, str]]:
    """读 output/{domain}/crawl_results.csv → list[dict]（utf-8-sig，去 BOM）。"""
    p = _OUTPUT_DIR / domain / "crawl_results.csv"
    if not p.is_file():
        return []
    with open(p, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _rewrite_csv(domain: str, rows: List[Dict[str, str]]) -> None:
    """按原 header 顺序重写 CSV（utf-8-sig，html 列多行会被正确引用）。"""
    p = _OUTPUT_DIR / domain / "crawl_results.csv"
    fieldnames = list(rows[0].keys()) if rows else []
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


# ── 一键入库（移植博宇 csv_to_sqlite：SCHEMA + url 唯一键 upsert） ──
_IMPORT_TABLE = "content_001"
_IMPORT_SCHEMA: Dict[str, str] = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "gsmc": "TEXT", "ywlx1": "TEXT", "ywlx2": "TEXT",
    "ywlx3": "TEXT", "ywlx4": "TEXT", "riqi": "TIMESTAMP",
    "title": "TEXT", "url": "TEXT UNIQUE", "html": "TEXT",
    "zdr": "TEXT", "timestamp": "TIMESTAMP", "ingested_at": "TIMESTAMP",
}


def _import_rows_to_sqlite(rows: List[Dict[str, Any]], db_path: str) -> Dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        cols_sql = ", ".join(f"{n} {d}" for n, d in _IMPORT_SCHEMA.items())
        conn.execute(f"CREATE TABLE IF NOT EXISTS {_IMPORT_TABLE} ({cols_sql})")
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({_IMPORT_TABLE})")}
        for name, dtype in _IMPORT_SCHEMA.items():
            if name == "id" or name in existing:
                continue
            conn.execute(f"ALTER TABLE {_IMPORT_TABLE} ADD COLUMN {name} {dtype}")
        columns = ["gsmc", "ywlx1", "ywlx2", "ywlx3", "ywlx4", "riqi",
                   "title", "url", "html", "zdr", "timestamp", "ingested_at"]
        placeholders = ", ".join("?" for _ in columns)
        col_list = ", ".join(columns)
        updates = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "url")
        sql = (f"INSERT INTO {_IMPORT_TABLE} ({col_list}) VALUES ({placeholders}) "
               f"ON CONFLICT(url) DO UPDATE SET {updates}")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        values = [
            (
                r.get("gsmc", ""), r.get("ywlx1", ""), r.get("ywlx2", ""),
                r.get("ywlx3", ""), r.get("ywlx4", ""), r.get("riqi", ""),
                r.get("title", ""), r.get("url", ""), r.get("html", ""),
                r.get("zdr", ""), r.get("timestamp", ""), now,
            )
            for r in rows
        ]
        conn.executemany(sql, values)
        conn.commit()
    finally:
        conn.close()
    return {
        "inserted": len(rows),
        "db_size": os.path.getsize(db_path) if os.path.exists(db_path) else 0,
    }


# ── LLM 配置中心 ──
@app.get("/llm/providers")
async def llm_providers(
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """把 config.json 收敛成博宇格式：单供应商 deepseek + 当前 model。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    cfg = _load_config()
    api_key = str(cfg.get("api_key", "") or "")
    model = str(cfg.get("model_name", "") or "") or "deepseek-v4-flash"
    masked = (api_key[:6] + "***" + api_key[-4:]) if api_key else ""
    providers = [{
        "id": "deepseek",
        "label": str(cfg.get("platform", "") or "DeepSeek"),
        "builtin": True,
        "has_key": bool(api_key),
        "masked": masked,
        "api_key_env": "DEEPSEEK_API_KEY",
        "models": [model] if model else ["deepseek-chat"],
    }]
    return {
        "providers": providers,
        "active_provider": "deepseek",
        "active_model": model,
        "active_provider_valid": True,
        "permission_warning": "",
    }


@app.post("/llm/config")
async def llm_config_save(
    req: LlmConfigRequest,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _check_api_key(_extract_key(x_api_key, authorization))
    cfg = _load_config()
    if req.active_model:
        cfg["model_name"] = req.active_model
    _save_config(cfg)
    return {"ok": True, "error": ""}


@app.post("/llm/keys")
async def llm_keys_save(
    req: LlmKeysRequest,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """保存 key → 写回 config.json 的 api_key（单供应商模式，任意 env 名都生效）。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    cfg = _load_config()
    saved_keys = cfg.setdefault("saved_keys", {})
    for env, key in req.keys.items():
        if key.strip():
            cfg["api_key"] = key.strip()
            saved_keys[env] = key.strip()
    _save_config(cfg)
    return {"ok": True, "error": ""}


@app.post("/llm/keys/test")
async def llm_keys_test(
    req: LlmKeyTestRequest,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """真实连通测试：config.base_url + (输入的 key 或已存 key) 发一次最小 chat 请求。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    import httpx

    cfg = _load_config()
    base_url = str(cfg.get("base_url", "") or "").rstrip("/")
    api_key = req.api_key.strip() or str(cfg.get("api_key", "") or "")
    model = req.model.strip() or str(cfg.get("model_name", "") or "") or "deepseek-v4-flash"
    if not base_url or not api_key:
        return {"ok": False, "detail": "缺少 base_url 或 API Key", "key_source": ""}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": [{"role": "user", "content": "ping"}],
                      "max_tokens": 8},
            )
            if resp.status_code < 400:
                return {"ok": True, "key_source": "input" if req.api_key.strip() else "stored"}
            return {"ok": False,
                    "detail": f"HTTP {resp.status_code}: {resp.text[:200]}", "key_source": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}", "key_source": ""}


@app.post("/llm/providers")
async def llm_provider_create(
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> JSONResponse:
    """只读收敛：boyushixi 为单供应商（config.json），不支持注册自定义供应商。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    return JSONResponse(status_code=400, content={
        "error": "当前为单供应商模式（配置存于 config.json），不支持新增自定义供应商"})


@app.delete("/llm/providers/{pid}")
async def llm_provider_delete(
    pid: str,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> JSONResponse:
    _check_api_key(_extract_key(x_api_key, authorization))
    return JSONResponse(status_code=400, content={"error": "内置供应商不可删除"})


# ── 结果浏览 / 保存 ──
@app.get("/orchestrator/result/{result_dir}/{filename}")
async def orchestrator_result(
    result_dir: str,
    filename: str,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Response:
    """按 md5 文件名反查 CSV 行，返回该页完整 HTML。编辑过优先返回 edited 副本。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    if not _DOMAIN_RE.match(result_dir) or not _FILEID_RE.match(filename):
        raise HTTPException(404, "非法路径")
    edited = _OUTPUT_DIR / result_dir / "edited" / filename
    if edited.is_file():
        return HTMLResponse(edited.read_text(encoding="utf-8", errors="replace"))
    rows = _read_csv_rows(result_dir)
    for row in rows:
        if _file_id(row.get("url", "")) == filename:
            return HTMLResponse(row.get("html", ""))
    raise HTTPException(404, f"未找到 {filename}")


@app.get("/orchestrator/cleaned/{result_dir}/{filename}")
async def orchestrator_cleaned(
    result_dir: str,
    filename: str,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Response:
    """boyushixi 无独立清洗产物（CSV 的 html 即提取后源码）→ 与 result 同源。"""
    return await orchestrator_result(result_dir, filename, x_api_key, authorization)


@app.post("/orchestrator/save/{result_dir}/{filename}")
async def orchestrator_save(
    result_dir: str,
    filename: str,
    req: SaveContentRequest,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """编辑保存：写回 CSV 对应行的 html 列 + 落盘 edited 副本。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    if not _DOMAIN_RE.match(result_dir) or not _FILEID_RE.match(filename):
        raise HTTPException(404, "非法路径")
    content = req.content or ""
    rows = _read_csv_rows(result_dir)
    hit = False
    for row in rows:
        if _file_id(row.get("url", "")) == filename:
            row["html"] = content
            hit = True
            break
    if not hit:
        raise HTTPException(404, f"未找到 {filename}")
    await asyncio.to_thread(_rewrite_csv, result_dir, rows)
    edited_dir = _OUTPUT_DIR / result_dir / "edited"
    edited_dir.mkdir(parents=True, exist_ok=True)
    (edited_dir / filename).write_text(content, encoding="utf-8")
    return {"ok": True}


# ── 分类 / 导出 / 入库 ──
@app.get("/orchestrator/classification")
async def orchestrator_classification(
    result_dir: str = "",
    leaf_only: str = "false",
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """从 CSV 提取 url → ywlx1-4 层级映射，供前端栏目树渲染。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    rows = _read_csv_rows(result_dir) if _DOMAIN_RE.match(result_dir) else []
    pages: Dict[str, Dict[str, str]] = {}
    for r in rows:
        url = r.get("url", "")
        if not url:
            continue
        if leaf_only == "true" and not (r.get("ywlx1") or "").strip():
            continue
        pages[url] = {
            "ywlx1": r.get("ywlx1", "") or "",
            "ywlx2": r.get("ywlx2", "") or "",
            "ywlx3": r.get("ywlx3", "") or "",
            "ywlx4": r.get("ywlx4", "") or "",
        }
    return {"pages": pages}


@app.get("/orchestrator/export_csv")
async def orchestrator_export_csv(
    result_dir: str = "",
    leaf_only: str = "false",
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Response:
    """导出原始 CSV（带 BOM）；leaf_only=true 时过滤无 ywlx1 的行。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    src = _OUTPUT_DIR / result_dir / "crawl_results.csv"
    if not _DOMAIN_RE.match(result_dir) or not src.is_file():
        raise HTTPException(404, "结果 CSV 不存在")
    if leaf_only == "true":
        rows = [r for r in _read_csv_rows(result_dir) if (r.get("ywlx1") or "").strip()]
        if not rows:
            rows = _read_csv_rows(result_dir)
        buf = io.StringIO()
        fieldnames = list(rows[0].keys()) if rows else []
        w = csv.DictWriter(buf, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
        content = "\ufeff" + buf.getvalue()
    else:
        content = src.read_bytes()
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="crawl_results.csv"'})


@app.post("/orchestrator/import_db")
async def orchestrator_import_db(
    result_dir: str = "",
    leaf_only: str = "false",
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """把 CSV 行灌入 content_001.db（url 唯一键 upsert），返回数据库地址。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    if not _DOMAIN_RE.match(result_dir) or not (_OUTPUT_DIR / result_dir).is_dir():
        raise HTTPException(404, "结果目录不存在")
    rows = _read_csv_rows(result_dir)
    if leaf_only == "true":
        rows = [r for r in rows if (r.get("ywlx1") or "").strip()]
    mapped = []
    for r in rows:
        riqi = r.get("tianextimejsj", "") or ""
        mapped.append({
            "gsmc": r.get("brwidcl_cpmc", "") or "",
            "ywlx1": r.get("ywlx1", "") or "", "ywlx2": r.get("ywlx2", "") or "",
            "ywlx3": r.get("ywlx3", "") or "", "ywlx4": r.get("ywlx4", "") or "",
            "riqi": riqi, "title": r.get("title", "") or "",
            "url": r.get("url", "") or "", "html": r.get("html", "") or "",
            "zdr": "", "timestamp": riqi,
        })
    if not mapped:
        raise HTTPException(404, "该批次没有可导入的行")
    db_name = "content_001.db"
    db_path = _OUTPUT_DIR / result_dir / db_name
    stats = await asyncio.to_thread(_import_rows_to_sqlite, mapped, str(db_path))
    gsmc = next((r["gsmc"] for r in mapped if r["gsmc"]), "")
    return {
        "inserted": stats["inserted"],
        "db_size": round(stats["db_size"] / 1024, 1),
        "db_abs_path": str(db_path.resolve()),
        "db_path": db_name,
        "result_dir": result_dir,
        "gsmc": gsmc,
    }


# ── SSE 适配层：/orchestrator/stream ──
def _node_state_for(node: str, req: OrchestratorStreamRequest, page_count: int,
                    domain: str, rows_count: int = 0) -> Dict[str, Any]:
    """给前端 nodeDetails 面板提供展示用的占位 state（boyushixi 无博宇对应指标）。"""
    if node == "probe":
        return {"probe": {"site_mode": req.site_mode, "status_code": 200,
                          "encoding": "utf-8", "is_spa_likely": False,
                          "robots_forbidden": False, "mode_conflict": ""}}
    if node == "derive":
        return {"strategy": {"content_filter": "auto", "pruning_threshold": 0.2,
                             "scan_full_page": True, "js_steps": 0}}
    if node == "crawl":
        return {"page_count": page_count}
    if node == "validate":
        return {"metrics": {"non_empty_ratio": 1.0, "noise_ratio": 0.0, "encoding_ok": True},
                "verdict": "pass", "feedback": "内容质量通过"}
    if node == "clean":
        return {"cleaned_dir": domain,
                "clean_stats": {"total": rows_count, "cleaned": rows_count,
                                "partial": 0, "skipped": 0, "failed": 0}}
    return {"needs_review": False, "result_dir": domain,
            "metrics": {"pages": rows_count}, "verdict": "pass", "feedback": ""}


def _sse_frame(evt: Dict[str, Any]) -> str:
    return f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"


async def _run_crawl_stream(task: Dict[str, Any], req: OrchestratorStreamRequest,
                            q: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
    """后台桥接：同步爬虫线程产事件 → call_soon_threadsafe → asyncio 队列 → SSE 帧。"""
    from main import run_langgraph_crawler

    domain = _domain_of(req.url)

    def emit(evt: Dict[str, Any]) -> None:
        loop.call_soon_threadsafe(q.put_nowait, evt)

    def _log(msg: str) -> None:
        task["logs"].append(str(msg))

    state = {"node": None, "saw_fetch": False, "attempt": 1, "fetched": 0,
             "saw_clean": False}

    def _progress(fetched: int, queue_len: int, url: str, phase: str) -> None:
        node = _NODE_BY_PHASE.get(phase, "crawl")
        if node == "clean":
            state["saw_clean"] = True
        if node != state["node"]:
            if state["node"] is not None:
                emit({"type": "node_end", "node": state["node"],
                      "state": _node_state_for(state["node"], req, state["fetched"], domain)})
            if node == "crawl" and state["saw_fetch"]:
                # evaluate 调整后重爬 = 博宇「换策略重试」语义
                state["attempt"] += 1
                emit({"type": "retry", "attempt": state["attempt"]})
            state["node"] = node
            emit({"type": "node_start", "node": node})
        if phase == "fetch":
            state["saw_fetch"] = True
            state["fetched"] = fetched
            emit({
                "type": "page_crawled", "url": url, "status_code": 200,
                "success": True, "title": "", "depth": 0,
                "page_count": fetched, "result_dir": domain,
                "filename": _file_id(url),
            })
        elif phase == "media":
            emit({"type": "cleaning_progress", "file": url, "status": "cleaned",
                  "current": fetched, "total": max(queue_len, fetched), "message": ""})
        task["progress"] = {"fetched": fetched, "queue_len": queue_len,
                            "url": url, "phase": phase}

    try:
        saved = await asyncio.to_thread(
            run_langgraph_crawler,
            req.url, concurrency=max(1, int(req.concurrent)),
            log_callback=_log, reset_memory=bool(req.no_cache),
            progress_callback=_progress,
        )
        task["saved"] = int(saved or 0)
        task["status"] = "done"
    except Exception as e:  # noqa: BLE001
        task["status"] = "failed"
        task["error"] = f"{type(e).__name__}: {e}"
        task["logs"].append(f"[api] 任务失败: {task['error']}")

    # 收尾：关掉当前节点 → 发 finalize（带 result_dir）→ done
    rows = _read_csv_rows(domain)
    if state.get("saw_clean") and state["node"] == "finalize":
        # _progress 阶段 CSV 尚未落盘，clean_stats.total 为 0；此处用真实行数补发
        emit({"type": "node_end", "node": "clean",
              "state": _node_state_for("clean", req, state["fetched"], domain,
                                       rows_count=len(rows))})
    if state["node"] is not None and state["node"] != "finalize":
        emit({"type": "node_end", "node": state["node"],
              "state": _node_state_for(state["node"], req, state["fetched"], domain,
                                       rows_count=len(rows))})
    if state["node"] != "finalize":
        emit({"type": "node_start", "node": "finalize"})
    emit({"type": "node_end", "node": "finalize",
          "state": _node_state_for("finalize", req, state["fetched"], domain,
                                   rows_count=len(rows))})
    emit({"type": "done", "status": "success" if task["status"] == "done" else "failed"})
    loop.call_soon_threadsafe(q.put_nowait, None)  # 哨兵：结束 SSE 流（与 emit 同序，排在 done 之后）


async def _sse_gen(q: asyncio.Queue) -> AsyncIterator[str]:
    while True:
        evt = await q.get()
        if evt is None:
            break
        yield _sse_frame(evt)


@app.post("/orchestrator/stream")
async def orchestrator_stream(
    req: OrchestratorStreamRequest,
    x_api_key: Optional[str] = Header(None),
    x_forwarded_for: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> StreamingResponse:
    """工作台爬取入口：单爬虫槽 + SSE 事件流（博宇 6 节点契约）。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    _check_stream_rate(_client_key(x_api_key, x_forwarded_for))
    busy = _running_task()
    if busy:
        raise HTTPException(409, f"已有爬取任务在运行（单爬虫槽），task_id={busy['task_id']}")
    if not req.url.strip():
        raise HTTPException(422, "url 不能为空")
    task_id = uuid.uuid4().hex[:12]
    task = {
        "task_id": task_id, "url": req.url, "status": "running",
        "saved": None, "error": None, "progress": None,
        "logs": deque(maxlen=_LOG_RING),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "finished_at": None,
    }
    _TASKS[task_id] = task
    _TASK_ORDER.append(task_id)
    _prune_tasks()
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    asyncio.create_task(_run_crawl_stream(task, req, q, loop))
    return StreamingResponse(_sse_gen(q), media_type="text/event-stream")


# ── 静态托管：工作台前端（挂在根路径，html=True 支持直接打开） ──
app.mount("/", StaticFiles(directory=str(_WORKBENCH_DIR), html=True), name="workbench")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
