# Development

## Project layout

```
├── agents/          # capability agents + safety layer + tool registry + budget
│                    #   + react (FC loop) + semdedup + vector_retriever + eval
├── graph/           # orchestration: workflow (Supervisor) / agents (9) / nodes / state
│                    #   + react_takeover (deep-degradation ReAct takeover)
├── tests/           # 271 unit tests (safety / plan / tools / ReAct / budgeting /
│                    #   dedup / RAG / eval metrics / FC eval path / graph smoke /
│                    #   golden loop / regression / takeover / breaker+rescue /
│                    #   MCP / API / distributed queue)
├── api/             # FastAPI service (server.py: submit/progress/results + SSE)
├── distributed/     # SQLite task queue + multiprocessing scheduler
├── tools/           # golden_check / compare_runs / static_check / rag_demo /
│                    #   mcp_server + mcp_client / gen_campus_report
├── reports/         # campus_report.{md,json} — offline quantified evidence
├── memory.py        # SQLite long-term memory (visited_urls / site_patterns)
├── schemas.py       # Pydantic models + logging
├── .github/         # CI (multi-version Python: pytest + static check + golden)
└── main.py          # CLI entry
```

## The three shells, one core

`run_langgraph_crawler` (in `graph/workflow.py`) is the single pipeline entry. The CLI (`main.py`), the tkinter GUI (`site_crawler_gui.py`) and the FastAPI service (`api/server.py`) are thin adapters around it. If you change the pipeline, all three shells change at once — prefer editing `graph/` over any single shell.

## Local workflow

```bash
pip install -r requirements.txt

# Run a crawl (one-liner)
python -c "import asyncio; from graph.workflow import run_crawler; asyncio.run(run_crawler('https://example.com', max_steps=3000))"

# Unit tests — must stay green
python -m pytest tests -q            # 271 passed

# Self-built static checker (ruff cross-verifies in CI)
python tools/static_check.py         # 62 files / 0 issues

# Offline golden evaluation
python tools/golden_check.py --offline --json

# Regression diff (primary metric worse → exit 1)
python tools/compare_runs.py baseline.json current.json
```

Or use the Makefile: `make test`, `make static`, `make golden`, `make check` (all three at once), `make web`, `make gui`, `make clean`.

## Testing conventions

- Every behavioral change ships with or updates a test. Cover the **degraded path** — the project's core promise is that everything still works with the LLM down.
- Tests never hit the network: fetch is injected/mocked; golden offline mode reads fixtures.
- New features get their own `tests/test_*.py` unless a natural home exists.

## Golden evaluation

`tools/golden_check.py` runs 3 template sites (portal / ecommerce `books.toscrape` / paged list `quotes.toscrape`) and reports P/R/F1 with a conservative `overlap = min(saved, expected)` accounting plus section recall. `--json` gives machine-readable output for `tools/compare_runs.py`.

The golden set is the **source of truth for regressions** — it must not be "adjusted to pass". Any pipeline change must re-run it and keep numbers stable or better.

## CI

`.github/workflows/ci.yml` runs on every push: pytest (+ coverage), the double static check (`tools/static_check.py` + ruff), golden verification, and advisory mypy. The environment is deliberately strict; run `make check` locally before pushing.

## Contributing

See [CONTRIBUTING.md](../../CONTRIBUTING.md) (or [CONTRIBUTING.zh-CN.md](../../CONTRIBUTING.zh-CN.md)) for PR conventions and design constraints. TL;DR: deterministic first, the breaker protects the crawl not the LLM, no heavyweight dependencies, conventional commit messages, PRs against `main`.
