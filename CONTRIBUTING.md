# Contributing to boyushixi

> [简体中文](CONTRIBUTING.zh-CN.md)

Thanks for the interest in contributing. boyushixi is a deterministic-first, multi-agent web harvester: 9 specialist agents orchestrated by a LangGraph supervisor, where the LLM is an *enhancer, never a dependency*. Everything below assumes you have the project running and want to change it responsibly.

## Prerequisites

- **Python** 3.11+
- **Playwright** with the Chromium browser (only needed for JS-rendered template sites)
- **Docker** (optional — for the FastAPI service and distributed scheduler; not required for local CLI dev)

## Local development

```bash
git clone https://github.com/LingMi1/langgraph-multi-agent-crawler.git
cd langgraph-multi-agent-crawler

# Dependencies
pip install -r requirements.txt

# Browser engine (only if you test JS-rendered template sites)
playwright install chromium

# Copy the environment template — API keys are optional, everything degrades gracefully
cp .env.example .env
```

Three shells, one core: the CLI, the tkinter GUI, and the FastAPI service all share the same `run_langgraph_crawler` entry — a change to the pipeline is a change to all three at once, so prefer editing `graph/` over any single shell.

## Running the pipeline

```bash
# CLI crawl (one-liner)
python -c "import asyncio; from graph.workflow import run_crawler; asyncio.run(run_crawler('https://example.com', max_steps=3000))"

# Desktop GUI (batch import from TXT + config + per-site crawl)
python site_crawler_gui.py

# REST service (submit / progress / results)
uvicorn api.server:app --host 0.0.0.0 --port 8000

# Distributed scheduler (SQLite queue + multiprocessing workers)
python distributed/scheduler.py enqueue urls.txt
python distributed/scheduler.py run-workers --workers 2
```

## Testing

```bash
# Full unit suite — must stay green
python -m pytest tests -q            # 271 passed

# Self-built static checker (62 files / 0 issues)
python tools/static_check.py

# Offline golden evaluation (P/R/F1 + section recall, machine-readable)
python tools/golden_check.py --offline --json

# Regression diff (primary metric worse → exit 1)
python tools/compare_runs.py baseline.json current.json
```

If a behavioral change is covered by an existing test file, extend it; otherwise add a new `tests/test_*.py`. Keep golden numbers honest: any change to the fetch/clean/extract pipeline must re-run `tools/golden_check.py` and the golden set must not be "adjusted to pass" — it is the source of truth for regressions.

## Linting

```bash
python tools/static_check.py         # self-built checker (ruff cross-verifies in CI)
ruff check .                         # if you use ruff locally
```

## Pull request guidelines

- Open PRs against `main`.
- Use **conventional commit** prefixes (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`, `revert:`) — the message body may be in English or Chinese.
- Keep PRs focused: one logical change per PR.
- Add or update tests for any behavioral change; run the full suite before opening the PR.

## CI requirements

Every PR must pass the gates in `.github/workflows/ci.yml` before merging: `pytest` (+ coverage), the double static check (`tools/static_check.py` + ruff), golden verification, and (advisory) mypy. Run `python -m pytest tests -q` and `python tools/static_check.py` locally to catch issues early — the CI environment is deliberately strict.

## Design constraints worth respecting

- **Deterministic first.** A node's default path must run without the LLM; the model only enters at judgment points under a semaphore. A change that makes an LLM call mandatory on the BFS hot path will be rejected.
- **The breaker protects the crawl, not the LLM.** Both LLM entry points share one run-level circuit breaker; callers must degrade, never block.
- **No new heavyweight dependencies.** The project deliberately runs on stdlib + a small set of pinned packages; prefer stdlib solutions (SQLite, `robotparser`, csv) unless there is a demonstrated need.
