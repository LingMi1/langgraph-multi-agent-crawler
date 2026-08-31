# Installation

## Requirements

- Python 3.11+
- Playwright + Chromium — *only* needed for JS-rendered template sites; everything else runs on plain `httpx`/`requests`
- Docker — optional, for the FastAPI service image

## 1. Install dependencies

```bash
pip install -r requirements.txt

# Browser engine (only for JS-rendered template sites)
playwright install chromium
```

## 2. Configure

Copy the environment template — every variable is optional; unset values simply degrade to deterministic paths:

```bash
cp .env.example .env
```

Key settings (`config.py` / `.env`):

| Variable | Default | Meaning |
|---|---|---|
| `CRAWLER_API_KEY` | unset (open, local dev) | when set, all API endpoints require `X-API-Key` |
| `CRAWLER_RESPECT_ROBOTS` | `true` | robots.txt compliance (wildcard `User-agent: *`) |
| `CRAWLER_TLS_VERIFY` | `true` | TLS cert verification on outbound crawl requests |
| `LLM_BASE_URL` / `LLM_API_KEY` | unset | primary LLM provider (DeepSeek-compatible OpenAI endpoint) |
| `LLM_BACKUP_BASE_URLS` | unset | failover providers, used after primary retries exhaust |
| `CRAWLER_CONCURRENCY` | 5 | per-crawl fetch semaphore size |

## 3. Run it

### CLI crawl

```bash
python -c "import asyncio; from graph.workflow import run_crawler; asyncio.run(run_crawler('https://example.com', max_steps=3000))"
```

### Desktop GUI

```bash
python site_crawler_gui.py
```

### FastAPI service

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

### Distributed scheduler (SQLite queue + multiprocessing workers)

```bash
python distributed/scheduler.py enqueue urls.txt
python distributed/scheduler.py run-workers --workers 2
python distributed/scheduler.py status
```

## 4. Docker

Build and run the API image (note: Playwright/system Chrome is *not* baked into the image — JS-rendered sites fall back to `httpx`; static/BS4 sites are fully functional):

```bash
docker build -t crawler-api .
docker run -p 8000:8000 -e CRAWLER_API_KEY=secret -v crawler_out:/app/output crawler-api
```

## Verify the install

```bash
python -m pytest tests -q            # 288 passed
python tools/static_check.py         # 64 files / 0 issues
python tools/golden_check.py --offline --json   # P/R/F1 report
```

See [development.md](development.md) for the full dev workflow.
