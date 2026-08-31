# API Reference

The FastAPI service lives in `api/server.py`. All endpoints live under one process; the service layer holds zero business logic — CLI, desktop GUI, and REST share the same `run_langgraph_crawler` core.

**Auth**: when `CRAWLER_API_KEY` is set, every endpoint requires the `X-API-Key` header (or `Authorization: Bearer <key>`), compared with `secrets.compare_digest`.

## Crawl tasks

### `POST /crawl`
Submit a site for crawling.

- Body (JSON): `{"url": "...", "concurrency": 5}`
- Response: `202 Accepted` with a `task_id`; `409` when the single crawl slot is busy; `429` when the client is throttled (6 submissions / 60s sliding window).

### `GET /tasks/{task_id}`
Task status and progress (includes the SSE event log).

### `GET /tasks/{task_id}/results`
Harvested pages for the task.

### `POST /orchestrator/stream`
SSE event stream used by the web workbench (`page_crawled`, `node_start`, `node_end`, `retry`, `done`, …). One request per crawl session; progress fields include `page_count`, `pending` and `total` for the dynamic progress bar.

## LLM configuration

| Endpoint | Purpose |
|---|---|
| `GET /llm/providers` | List configured providers |
| `POST /llm/providers` | Add/update a provider |
| `DELETE /llm/providers/{pid}` | Remove a provider |
| `POST /llm/config` | Apply provider routing config (three-way sync: web / desktop GUI / CLI) |
| `POST /llm/keys` | Store an API key (server-side) |
| `POST /llm/keys/test` | Connectivity test against a provider |

The routing config is the single source of truth: the FastAPI service, the desktop GUI, and the CLI all read the same applied config.

## Results & history

### `GET /orchestrator/result/{result_dir}/{filename}`
Raw page content for one harvested page (resolved from the run's CSV by `_file_id`, not from disk). Duplicate-content rows return an explicit "duplicate, not stored separately" notice.

### `GET /orchestrator/cleaned/{result_dir}/{filename}`
Cleaned (rules-applied) version of a page, when a cleaned output directory exists for the run.

### `POST /orchestrator/save/{result_dir}/{filename}`
Persist an edited page back to the run directory. Body: `{"content": "..."}`.

### `GET /orchestrator/classification?result_dir=…&leaf_only=…`
Per-page metadata: `ywlx1-4` section hierarchy, real title, and duplicate marker — used to build the left-hand tree in the workbench.

### `GET /orchestrator/export_csv`
Export the run's `content_001` CSV (section columns included, for DB import).

### `GET /orchestrator/output_zip?result_dir=…&leaf_only=true|false`
Server-side bundle of the run's output directory: `<domain>/<section>/<title>.html` + `crawl_results.csv`. Duplicate placeholder rows are excluded. This powers the workbench's "save all results" button (browser picks a directory, the zip is unpacked into it; unsupported browsers download the zip directly).

### `POST /orchestrator/import_db`
Import a run into the database (one-click from the workbench).

## History (server-side SQLite)

| Endpoint | Purpose |
|---|---|
| `GET /history` | List crawl-history records (runs + site pages) |
| `POST /history` | Save a completed run snapshot |
| `GET /history/{hid}` | One record (pages + metrics) |
| `DELETE /history/{hid}` | Delete one record |
| `DELETE /history` | Clear all history |

## Streaming chat

### `POST /chat/stream`
SSE streaming endpoint for the LLM pipeline's `chat_stream` path (multi-provider failover under the run-level circuit breaker).

## Error conventions

- `404` — missing result dir / file / history record
- `409` — crawl slot busy
- `429` — client throttled (rate limit)
- `401` — API key missing/mismatch (when auth is enabled)
