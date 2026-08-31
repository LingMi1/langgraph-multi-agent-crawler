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
import zipfile
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

# 8 phase → 9 DAG 节点映射（scout/navigate/fetch(+rescue)/evaluate/config_adjust/code_gen/react/media/storage）
_NODE_BY_PHASE = {
    "scout": "scout", "navigate": "navigate", "fetch": "fetch_extract",
    "rescue_locate": "fetch_extract", "rescue": "fetch_extract",
    "evaluate": "evaluate", "config_adjust": "config_adjust",
    "code_gen": "code_gen", "react": "react",
    "media": "media_processor", "storage": "storage",
}
_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9.\-:]+$")   # 域名（含端口）防路径穿越
_FILEID_RE = re.compile(r"^[0-9a-f]{16}\.html$")  # 文件名=url 的 md5 前 16 位
_DUP_HASH_RE = re.compile(r"hash=([0-9a-f]+)")    # 内容去重占位行的指纹


class OrchestratorStreamRequest(BaseModel):
    """工作台 /orchestrator/stream 请求体（博宇前端契约）。

    urls 为批量种子列表（每行一个，对齐 GUI 的 txt 导入语义）；
    url 保留兼容单 URL 调用，合并后去重。
    """
    url: str = Field("", description="目标网站首页 URL（单 URL 兼容）")
    urls: List[str] = Field(default_factory=list, description="批量目标网站 URL 列表")
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


class LlmProviderCreateRequest(BaseModel):
    """新增自定义 OpenAI 兼容供应商。"""
    label: str = Field("")
    base_url: str = Field("")
    api_key: str = Field("")
    models: str = Field("")


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


# ── LLM 多供应商注册表（内置预设 base_url 锁定，仅 key/模型可改） ──
_BUILTIN_PROVIDERS: List[Dict[str, Any]] = [
    {"id": "zhipu", "label": "智谱 GLM", "builtin": True,
     "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "api_key_env": "ZHIPU_API_KEY",
     "models": ["glm-4-flash", "glm-5.2", "glm-5.1", "glm-4", "glm-3-turbo"]},
    {"id": "deepseek", "label": "DeepSeek", "builtin": True,
     "base_url": "https://api.deepseek.com",
     "api_key_env": "DEEPSEEK_API_KEY",
     "models": ["deepseek-chat", "deepseek-reasoner"]},
    {"id": "openai", "label": "OpenAI", "builtin": True,
     "base_url": "https://api.openai.com/v1",
     "api_key_env": "OPENAI_API_KEY",
     "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1-mini"]},
]


def _mask_key(key: str) -> str:
    if not key:
        return ""
    return (key[:6] + "***" + key[-4:]) if len(key) > 10 else "***"


def _get_provider_list(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """内置预设 + 自定义供应商合并。key 解析：saved_keys[env] 优先，
    active 供应商回退顶层 api_key（兼容旧 config.json 直接填 key 的用法）。
    返回的条目含明文 "key"（内部用），对外接口需剔除。
    """
    saved = cfg.get("saved_keys") or {}
    active_pid = cfg.get("active_provider") or "deepseek"
    top_key = str(cfg.get("api_key", "") or "")
    out: List[Dict[str, Any]] = []
    for p in _BUILTIN_PROVIDERS:
        env = p["api_key_env"]
        key = saved.get(env, "")
        if p["id"] == active_pid and not key:
            key = top_key
        out.append({**p, "key": key})
    for c in cfg.get("llm_providers") or []:
        env = c.get("api_key_env", "")
        key = saved.get(env, "")
        if c.get("id") == active_pid and not key:
            key = top_key
        out.append({**c, "builtin": False, "key": key})
    return out


def _find_provider(cfg: Dict[str, Any], pid: str) -> Optional[Dict[str, Any]]:
    return next((p for p in _get_provider_list(cfg) if p["id"] == pid), None)


def _sync_llm_config() -> None:
    """把 config.json 的 LLM 配置同步到运行时（网页端配置中心真正生效）。

    三步：① 写 env（子进程/其他消费者可见）；② 直接覆写 config 模块属性
    （config.py 常量在 import 时定格，而 agents.llm_pipeline._providers 是调用时
    读取 config.DEEPSEEK_*，覆写后立即生效）；③ reset_llm() 丢弃已缓存客户端。
    """
    cfg = _load_config()
    pairs = {
        "api_key": "DEEPSEEK_API_KEY",
        "base_url": "DEEPSEEK_BASE_URL",
        "model_name": "DEEPSEEK_MODEL",
    }
    values = {}
    for field, env_name in pairs.items():
        val = str(cfg.get(field, "") or "").strip()
        if not val:
            continue
        values[env_name] = val
        if not os.environ.get(env_name):
            os.environ[env_name] = val
    if values:
        try:
            import config as _crawler_config

            for env_name, val in values.items():
                setattr(_crawler_config, env_name, val)
            from agents.llm_pipeline import reset_llm

            reset_llm()
            # 切换供应商后解除本 run 熔断 → 正在进行的爬取用新配置继续（切换 LLM 续跑）
            from agents.breaker import llm_breaker

            llm_breaker.reset()
        except Exception:  # noqa: BLE001 — 桥接失败不阻断配置保存
            pass


_sync_llm_config()  # 模块加载即同步，早于 main 的延迟 import


# ── 域名 / 结果目录工具 ──
def _domain_of(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else "https://" + url)
    return parsed.netloc or ""


def _normalize_urls(req: "OrchestratorStreamRequest") -> List[str]:
    """合并 urls + url（单 URL 兼容），去重（去尾部斜杠）、跳过注释/空行/非 http 行。"""
    raw: List[str] = list(req.urls or [])
    if req.url.strip():
        raw.append(req.url)
    seen: set = set()
    out: List[str] = []
    for item in raw:
        for chunk in re.split(r"[\s,，;；]+", item.strip()):
            chunk = chunk.strip()
            if not chunk or chunk.startswith("#"):
                continue
            if not chunk.startswith(("http://", "https://")):
                continue
            key = chunk.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            out.append(chunk)
    return out


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


# ── LLM 配置中心（多供应商：内置 3 家 + 自定义 OpenAI 兼容） ──
@app.get("/llm/providers")
async def llm_providers(
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """返回全部供应商（内置+自定义，key 只回掩码）+ 当前 active 供应商/模型。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    cfg = _load_config()
    providers = []
    for p in _get_provider_list(cfg):
        providers.append({
            "id": p["id"], "label": p["label"], "builtin": bool(p.get("builtin")),
            "api_key_env": p["api_key_env"], "base_url": p["base_url"],
            "models": p.get("models") or [],
            "has_key": bool(p["key"]), "masked": _mask_key(p["key"]),
        })
    active_pid = cfg.get("active_provider") or "deepseek"
    active_model = str(cfg.get("model_name", "") or "") or "deepseek-v4-flash"
    active = _find_provider(cfg, active_pid)
    return {
        "providers": providers,
        "active_provider": active_pid,
        "active_model": active_model,
        "active_provider_valid": active is not None,
        "permission_warning": "",
    }


@app.post("/llm/config", response_model=None)
async def llm_config_save(
    req: LlmConfigRequest,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Union[Dict[str, Any], JSONResponse]:
    """保存 active 供应商 + 模型 → 顶层 base_url/api_key/platform 与该供应商对齐（爬虫主用）。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    cfg = _load_config()
    if req.active_provider:
        provider = _find_provider(cfg, req.active_provider)
        if provider is None:
            return JSONResponse(status_code=400,
                                content={"error": f"供应商 {req.active_provider} 不存在"})
        saved = cfg.setdefault("saved_keys", {})
        # 切换前捕获旧 active 的 key（避免只存在于顶层的 key 丢失）
        old_pid = cfg.get("active_provider") or "deepseek"
        old_provider = _find_provider(cfg, old_pid)
        if old_provider and cfg.get("api_key") and not saved.get(old_provider["api_key_env"]):
            saved[old_provider["api_key_env"]] = str(cfg["api_key"])
        # 切到新供应商：base_url/平台名取自该供应商；api_key 取该供应商已存 key
        cfg["active_provider"] = provider["id"]
        cfg["base_url"] = provider["base_url"]
        cfg["api_key"] = saved.get(provider["api_key_env"], "")
        cfg["platform"] = provider["label"]
    if req.active_model:
        cfg["model_name"] = req.active_model.strip()
    _save_config(cfg)
    _sync_llm_config()
    return {"ok": True, "error": ""}


@app.post("/llm/keys", response_model=None)
async def llm_keys_save(
    req: LlmKeysRequest,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """按供应商 env 保存 key 到 saved_keys；若是当前 active 供应商 → 同步顶层 api_key。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    cfg = _load_config()
    saved = cfg.setdefault("saved_keys", {})
    active_pid = cfg.get("active_provider") or "deepseek"
    for env, key in req.keys.items():
        key = key.strip()
        if not key:
            continue
        saved[env] = key
        provider = next((p for p in _get_provider_list(cfg)
                         if p["api_key_env"] == env), None)
        if provider and provider["id"] == active_pid:
            cfg["api_key"] = key
    _save_config(cfg)
    _sync_llm_config()
    return {"ok": True, "error": ""}


@app.post("/llm/keys/test", response_model=None)
async def llm_keys_test(
    req: LlmKeyTestRequest,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """真实连通测试：目标供应商 base_url + (输入的 key 或已存 key) 发一次最小 chat 请求。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    import httpx

    cfg = _load_config()
    active_pid = cfg.get("active_provider") or "deepseek"
    target_pid = req.provider or active_pid
    provider = _find_provider(cfg, target_pid)
    base_url = (provider or {}).get("base_url") or str(cfg.get("base_url", "") or "")
    env = (provider or {}).get("api_key_env", "DEEPSEEK_API_KEY")
    saved = cfg.get("saved_keys") or {}
    api_key = req.api_key.strip() or saved.get(env, "") or (
        str(cfg.get("api_key", "") or "") if target_pid == active_pid else ""
    )
    model = req.model.strip() or str(cfg.get("model_name", "") or "") or (
        (provider or {}).get("models") or ["deepseek-chat"])[0]
    if not base_url or not api_key:
        return {"ok": False, "detail": "缺少 base_url 或 API Key", "key_source": ""}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
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


@app.post("/llm/providers", response_model=None)
async def llm_provider_create(
    req: LlmProviderCreateRequest,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Union[Dict[str, Any], JSONResponse]:
    """新增自定义 OpenAI 兼容供应商（OpenAI 公共协议）。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    cfg = _load_config()
    label = (req.label or "").strip()
    base_url = (req.base_url or "").strip().rstrip("/")
    if not label or not base_url:
        return JSONResponse(status_code=400, content={"error": "请填写名称和接口地址"})
    if not base_url.startswith(("http://", "https://")):
        return JSONResponse(status_code=400, content={"error": "接口地址需以 http(s):// 开头"})
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", label).strip("_").lower() or "custom"
    pid = slug
    existing = {p["id"] for p in _get_provider_list(cfg)}
    n = 1
    while pid in existing:
        n += 1
        pid = f"{slug}_{n}"
    env = "LLM_CUSTOM_" + pid.upper()
    models = [m.strip() for m in re.split(r"[,，]", req.models or "") if m.strip()] \
        or ["deepseek-chat"]
    providers = cfg.setdefault("llm_providers", [])
    providers.append({
        "id": pid, "label": label, "base_url": base_url,
        "api_key_env": env, "models": models,
    })
    if (req.api_key or "").strip():
        cfg.setdefault("saved_keys", {})[env] = req.api_key.strip()
    _save_config(cfg)
    _sync_llm_config()
    return {"ok": True, "provider": {"id": pid, "label": label}}


@app.delete("/llm/providers/{pid}", response_model=None)
async def llm_provider_delete(
    pid: str,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Union[Dict[str, Any], JSONResponse]:
    """删除自定义供应商（含其 key）；内置或当前使用中的供应商不可删。"""
    _check_api_key(_extract_key(x_api_key, authorization))
    cfg = _load_config()
    providers = cfg.get("llm_providers") or []
    target = next((p for p in providers if p["id"] == pid), None)
    if target is None:
        return JSONResponse(status_code=400, content={"error": "仅支持删除自定义供应商"})
    if cfg.get("active_provider") == pid:
        return JSONResponse(status_code=400,
                            content={"error": "当前正在使用的供应商不可删除，请先在「当前模型」切换到其他供应商"})
    cfg["llm_providers"] = [p for p in providers if p["id"] != pid]
    (cfg.get("saved_keys") or {}).pop(target.get("api_key_env", ""), None)
    _save_config(cfg)
    _sync_llm_config()
    return {"ok": True}


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
            html = row.get("html", "")
            # 内容去重占位行：返回明确提示页（否则前端正文区一片空白，像功能坏了）
            if html.lstrip().startswith("<!-- duplicate"):
                m = _DUP_HASH_RE.search(html)
                h = m.group(1) if m else "?"
                tip = (
                    '<div style="font-family:system-ui,sans-serif;padding:28px 24px;color:#64748b;line-height:1.8;">'
                    '<p style="margin:0 0 8px;font-size:15px;color:#334155;font-weight:600;">这一页是重复内容，没有单独存储</p>'
                    '<p style="margin:0;font-size:13px;">爬虫按「标题+正文」做内容去重，与站内另一页完全相同的页面只保留一份，'
                    f'此条是重复记录（内容指纹 <code>{h}</code>）。</p>'
                    '<p style="margin:8px 0 0;font-size:13px;">请在左侧列表查看同栏目下其他页面。</p></div>'
                )
                return HTMLResponse(tip)
            return HTMLResponse(html)
    raise HTTPException(404, f"未找到 {filename}")


@app.get("/orchestrator/cleaned/{result_dir}/{filename}")
async def orchestrator_cleaned(
    result_dir: str,
    filename: str,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Response:
    """本项目无独立清洗产物（CSV 的 html 即提取后源码）→ 与 result 同源。"""
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
            # 真实标题：SSE 事件不携带 title，前端树用它补全叶子显示
            "title": r.get("title", "") or "",
            # 重复内容行标记（前端树里置灰，点开显示去重提示）
            "dup": "true" if (r.get("html") or "").lstrip().startswith("<!-- duplicate") else "false",
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


# 路径段清洗规则与 graph/nodes.py:_save_html_file 保持一致（栏目/标题 → 合法目录/文件名）
_ZIP_NAME_RE = re.compile(r'[\\/:*?"<>|]')


def _zip_safe_seg(text: str, max_len: int = 60) -> str:
    """清洗 zip 内的目录/文件名段：非法字符→下划线，压缩空白，截断。"""
    seg = re.sub(r'[\s\-—·:：_]*第\s*\d+\s*页[\s\-—·:：_]*$', '', (text or '').strip())
    seg = re.sub(r'\s+', ' ', seg)
    seg = _ZIP_NAME_RE.sub('_', seg).strip('._ ')[:max_len]
    return seg


@app.get("/orchestrator/output_zip")
async def orchestrator_output_zip(
    result_dir: str = "",
    leaf_only: str = "false",
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Response:
    """打包全部爬取结果为 zip（与小工具的 output 目录结构一致）。

    结构: <域名>/栏目1/栏目2/…/标题.html + crawl_results.csv。
    排除重复页：内容指纹判重产生的 <!-- duplicate …--> 占位行不导出。
    """
    _check_api_key(_extract_key(x_api_key, authorization))
    if not _DOMAIN_RE.match(result_dir):
        raise HTTPException(404, "结果目录不存在")

    rows = _read_csv_rows(result_dir)
    if not rows:
        raise HTTPException(404, "结果 CSV 不存在")
    if leaf_only == "true":
        leaf_rows = [r for r in rows if (r.get("ywlx1") or "").strip()]
        if leaf_rows:
            rows = leaf_rows

    buf = io.BytesIO()
    included = 0
    used_paths: set = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in rows:
            html = (r.get("html") or "").lstrip()
            if html.startswith("<!--"):
                continue  # 重复/跳过占位行（无内容文件），不导出
            sub_dirs = [_zip_safe_seg(r.get(f"ywlx{i}", "")) for i in (1, 2, 3, 4)]
            sub_dirs = [d for d in sub_dirs if d]
            name = _zip_safe_seg(r.get("title", "")) or "page"
            rel = "/".join(sub_dirs + [f"{name}.html"])
            # 同栏目同标题冲突 → 标题_2/标题_3 递增（zip 内确定性去重）
            base_rel, n = rel, 2
            while rel in used_paths:
                rel = base_rel.replace(".html", f"_{n}.html")
                n += 1
            used_paths.add(rel)
            zf.writestr(f"{result_dir}/{rel}", r.get("html") or "")
            included += 1
        # 过滤后的 CSV（与导出的 html 集合一致：无重复占位行）
        csv_buf = io.StringIO()
        fieldnames = list(rows[0].keys())
        w = csv.DictWriter(csv_buf, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            if not (r.get("html") or "").lstrip().startswith("<!--"):
                w.writerow(r)
        zf.writestr(f"{result_dir}/crawl_results.csv", "\ufeff" + csv_buf.getvalue())
    if included == 0:
        raise HTTPException(404, "没有可导出的页面（全部为重复/占位行）")

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{result_dir}_results.zip"'},
    )


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


# ── 历史记录（SQLite 持久化，跨浏览器/重启保留） ──
_HISTORY_DB = PROJECT_ROOT / "history.db"


def _history_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_HISTORY_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS history ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT NOT NULL,"
        " payload TEXT NOT NULL)"
    )
    return conn


@app.get("/history")
async def history_list(
    limit: int = 100,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _check_api_key(_extract_key(x_api_key, authorization))
    conn = _history_conn()
    try:
        rows = conn.execute(
            "SELECT id, created_at, payload FROM history ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
    finally:
        conn.close()
    items = []
    for rid, created_at, payload in rows:
        try:
            p = json.loads(payload)
        except Exception:
            p = {}
        items.append({
            "id": rid,
            "created_at": created_at,
            "urls": p.get("urls", []),
            "first_url": (p.get("urls") or [""])[0],
            "status": p.get("status", ""),
            "page_count": p.get("page_count", 0),
            "site_mode": p.get("site_mode", "auto"),
            "result_dirs": p.get("result_dirs", []),
        })
    return {"items": items}


@app.get("/history/{hid}")
async def history_detail(
    hid: int,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _check_api_key(_extract_key(x_api_key, authorization))
    conn = _history_conn()
    try:
        row = conn.execute("SELECT payload FROM history WHERE id=?", (hid,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "历史记录不存在")
    try:
        payload = json.loads(row[0])
    except Exception:
        raise HTTPException(500, "历史记录损坏")
    return {"id": hid, **payload}


@app.post("/history")
async def history_create(
    body: Dict[str, Any],
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _check_api_key(_extract_key(x_api_key, authorization))
    payload = {
        "created_at": body.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "urls": body.get("urls", []),
        "status": body.get("status", ""),
        "page_count": body.get("page_count", 0),
        "site_mode": body.get("site_mode", "auto"),
        "result_dirs": body.get("result_dirs", []),
        "pages": body.get("pages", []),
        "sites": body.get("sites", []),
        "metrics": body.get("metrics"),
        "cleaned_dir": body.get("cleaned_dir", ""),
        "clean_stats": body.get("clean_stats", {}),
        "edits": body.get("edits", {}),
    }
    conn = _history_conn()
    try:
        cur = conn.execute(
            "INSERT INTO history (created_at, payload) VALUES (?, ?)",
            (payload["created_at"], json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()
        hid = cur.lastrowid
    finally:
        conn.close()
    conn = _history_conn()
    try:
        conn.execute(
            "DELETE FROM history WHERE id NOT IN "
            "(SELECT id FROM history ORDER BY id DESC LIMIT 100)"
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "id": hid}


@app.delete("/history/{hid}")
async def history_delete(
    hid: int,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _check_api_key(_extract_key(x_api_key, authorization))
    conn = _history_conn()
    try:
        conn.execute("DELETE FROM history WHERE id=?", (hid,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/history")
async def history_clear(
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _check_api_key(_extract_key(x_api_key, authorization))
    conn = _history_conn()
    try:
        conn.execute("DELETE FROM history")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ── SSE 适配层：/orchestrator/stream ──
def _node_state_for(node: str, req: OrchestratorStreamRequest, page_count: int,
                    domain: str, rows_count: int = 0) -> Dict[str, Any]:
    """给前端 nodeDetails 面板提供展示用的占位 state（对齐 9 个真实 Agent 节点）。"""
    if node == "scout":
        return {"probe": {"site_mode": req.site_mode, "status_code": 200,
                          "encoding": "utf-8", "is_spa_likely": False,
                          "robots_forbidden": False, "mode_conflict": ""}}
    if node == "navigate":
        return {"strategy": {"content_filter": "auto", "pruning_threshold": 0.2,
                             "scan_full_page": True, "js_steps": 0}}
    if node == "fetch_extract":
        return {"page_count": page_count}
    if node == "evaluate":
        return {"metrics": {"non_empty_ratio": 1.0, "noise_ratio": 0.0, "encoding_ok": True},
                "verdict": "pass", "feedback": "内容质量通过"}
    if node == "config_adjust":
        return {"adjustment": {"count": 1, "needs_js_render": False,
                               "recommended_ua": ""}}
    if node == "code_gen":
        return {"rule_gen": {"attempted": True, "rule_count": 0}}
    if node == "react":
        return {"react": {"decision": "giveup", "summary": "深降级自主接管"}}
    if node == "media_processor":
        return {"cleaned_dir": domain,
                "clean_stats": {"total": rows_count, "cleaned": rows_count,
                                "partial": 0, "skipped": 0, "failed": 0}}
    return {"needs_review": False, "result_dir": domain,
            "metrics": {"pages": rows_count}, "verdict": "pass", "feedback": ""}


def _sse_frame(evt: Dict[str, Any]) -> str:
    return f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"


async def _run_crawl_stream(task: Dict[str, Any], req: OrchestratorStreamRequest,
                            q: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
    """后台桥接：同步爬虫线程产事件 → call_soon_threadsafe → asyncio 队列 → SSE 帧。

    多 URL 时逐个串行爬取：每个网站一轮完整的 DAG（probe→…→finalize），
    轮间发 site_start 事件供前端区分站点。
    """
    from main import run_langgraph_crawler

    urls = _normalize_urls(req)
    total = len(urls)

    def emit(evt: Dict[str, Any]) -> None:
        loop.call_soon_threadsafe(q.put_nowait, evt)

    def _log(msg: str) -> None:
        task["logs"].append(str(msg))

    any_failed = False
    llm_failed_emitted = False

    def _notify_llm_failed() -> None:
        """熔断打开 → 一次性通知前端提示用户切换供应商（爬取本身继续降级运行）。"""
        nonlocal llm_failed_emitted
        if llm_failed_emitted:
            return
        try:
            from agents.breaker import llm_breaker
        except Exception:
            return
        if llm_breaker.open:
            llm_failed_emitted = True
            emit({"type": "llm_failed", "detail": (llm_breaker.reason or "")[:300]})

    for i, url in enumerate(urls):
        domain = _domain_of(url)
        state = {"node": None, "saw_fetch": False, "attempt": 1, "fetched": 0,
                 "saw_clean": False}

        emit({"type": "site_start", "url": url, "index": i + 1, "total": total})

        def _progress(fetched: int, queue_len: int, u: str, phase: str,
                      _state: Dict[str, Any] = state, _domain: str = domain) -> None:
            _notify_llm_failed()  # 每次进度回调顺带检查 LLM 熔断 → 通知前端
            node = _NODE_BY_PHASE.get(phase, "crawl")
            if node == "media_processor":
                _state["saw_clean"] = True
            if node != _state["node"]:
                if _state["node"] is not None:
                    emit({"type": "node_end", "node": _state["node"],
                          "state": _node_state_for(_state["node"], req, _state["fetched"], _domain)})
                if node == "fetch_extract" and _state["saw_fetch"]:
                    # evaluate 调整后重抓 = 「换策略重试」语义（回跳 navigate→fetch_extract）
                    _state["attempt"] += 1
                    emit({"type": "retry", "attempt": _state["attempt"]})
                _state["node"] = node
                emit({"type": "node_start", "node": node})
            if phase == "fetch":
                _state["saw_fetch"] = True
                _state["fetched"] = fetched
                emit({
                    "type": "page_crawled", "url": u, "status_code": 200,
                    "success": True, "title": "",
                    "page_count": fetched, "result_dir": _domain,
                    "filename": _file_id(u),
                    # 动态分母：已抓 + 队列剩余（边爬边发现新页，总量会增长）
                    "pending": queue_len,
                    "total": fetched + queue_len,
                })
            elif phase == "media":
                emit({"type": "cleaning_progress", "file": u, "status": "cleaned",
                      "current": fetched, "total": max(queue_len, fetched), "message": ""})
            task["progress"] = {"fetched": fetched, "queue_len": queue_len,
                                "url": u, "phase": phase}

        try:
            saved = await asyncio.to_thread(
                run_langgraph_crawler,
                url, concurrency=max(1, int(req.concurrent)),
                log_callback=_log, reset_memory=bool(req.no_cache),
                progress_callback=_progress,
            )
            task["saved"] = int(saved or 0) + int(task.get("saved") or 0)
        except Exception as e:  # noqa: BLE001 — 单站失败不阻断后续站点
            any_failed = True
            task["error"] = f"{type(e).__name__}: {e}"
            task["logs"].append(f"[api] {url} 爬取失败: {task['error']}")

        # 该站收尾：关掉当前节点 → storage（带 result_dir）→ 下一站
        _notify_llm_failed()
        rows = _read_csv_rows(domain)
        if state.get("saw_clean") and state["node"] == "storage":
            # _progress 阶段 CSV 尚未落盘，clean_stats.total 为 0；此处用真实行数补发
            emit({"type": "node_end", "node": "media_processor",
                  "state": _node_state_for("media_processor", req, state["fetched"], domain,
                                           rows_count=len(rows))})
        if state["node"] is not None and state["node"] != "storage":
            emit({"type": "node_end", "node": state["node"],
                  "state": _node_state_for(state["node"], req, state["fetched"], domain,
                                           rows_count=len(rows))})
        if state["node"] != "storage":
            emit({"type": "node_start", "node": "storage"})
        emit({"type": "node_end", "node": "storage",
              "state": _node_state_for("storage", req, state["fetched"], domain,
                                       rows_count=len(rows))})

    task["status"] = "done" if not any_failed else "failed"
    emit({"type": "done", "status": "success" if not any_failed else "failed"})
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
    if not _normalize_urls(req):
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
