# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Bilingual community docs: `CONTRIBUTING` / `CODE_OF_CONDUCT` (English + 简体中文), a `docs/en` and `docs/zh-CN` doc suite (architecture / installation / API / development), README badges, `Makefile`, and `.dockerignore`.

## [0.2.0] - 2026-08-31

### Added
- **Web workbench** (`api/static/workbench`) — four-step crawl wizard (reconnaissance → crawl → clean → results) with live SSE progress, a view-results page with section tree + preview (render / raw HTML), history drawer, one-click DB import, and "save all results" (server-side output zip → pick a directory → unpack, or direct zip download).
- **LLM config center** (`api/server.py` + GUI) — multi-provider (Zhipu / DeepSeek / OpenAI / custom), key management, connectivity test; routing config is the single source of truth synced across web / desktop GUI / CLI.
- **Server-side crawl history** — SQLite-backed records with site pages and metrics; view / delete / clear from the workbench.
- **Distributed scheduler** (`distributed/`) — stdlib SQLite task queue + multiprocessing workers: `BEGIN IMMEDIATE` atomic claim, lease + heartbeat + crash recovery, attempts-bounded retry.
- **REST service** — `POST /crawl`, `GET /tasks/{id}`, `GET /tasks/{id}/results`, `POST /chat/stream` SSE, X-API-Key auth + client throttling.

### Fixed
- Crawl of the same site no longer duplicates history entries across rounds; a config-adjust re-crawl clears content fingerprints so pages are not falsely marked as duplicates.
- Workbench view-results: raw HTML tab rewritten as plain text (CodeMirror multi-instance crash eliminated); tree shows real page titles; duplicate pages are greyed out with an explicit notice instead of blank content.

## [0.1.0] - 2026-08-31

### Added
- **Multi-agent web harvester** — 9 specialist agents (`graph/agents.py`) orchestrated by a LangGraph supervisor: scout / navigate / fetch-extract / evaluate / config-adjust / code-gen / react-takeover / media / storage.
- **Deterministic-first pipeline** — every node runs without the LLM by default; the LLM enters only at judgment points under a semaphore; run-level circuit breaker, batched post-hoc rescue, budget-triggered context compaction.
- **Cleaning engine** — `_strip_nav_noise` rules (9+ steps: nav containers, breadcrumbs, floating service bars, iYong/eWebSoft template menus, footer residuals, QR aggregator pages), anti-leech image download with three-step referer fallback, global image dedup.
- **Site memory** (`memory.py`) — SQLite `site_patterns`: a second crawl of the same site skips re-reconnaissance.
- **Offline golden evaluation** (`tools/golden_check.py`) — P/R/F1 + section recall over 3 template sites; regression diff (`tools/compare_runs.py`) gates CI.
- **Safety & compliance** — three-layer prompt-injection defense, robots.txt on by default, TLS verify on by default, self-imposed frequency limiting.
- **Three shells, one core** — CLI (`main.py`), tkinter GUI (`site_crawler_gui.py`), FastAPI service (`api/server.py`) share one `run_langgraph_crawler` entry.

### Measured milestones (development history)

- A full crawl of `zztzmjg.com` went from stalled overnight at 86 pages to **85 seconds / 3 LLM calls** after the circuit breaker + batched rescue were introduced.
- The evaluation loop fired **12 config adjustments across 4 runs** — saved pages on `xnjzgc.cn` went 1→98, on `zztzmjg.com` 3→84.
- Site-memory warm start reached a **100% hit rate** on second crawls of the same site.
- The distributed scheduler batch-ran **3 real sites × 2 workers → all `done`, `attempts=1` each** (exactly-once measured).
