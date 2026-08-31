# LangGraph Multi-Agent Crawler — multi-agent web harvester
# Usage: make <target>   (Windows users: install GNU make, e.g. choco install make)

.PHONY: setup test static links golden web gui scheduler clean check e2e precommit

PYTHON ?= python
UVICORN ?= uvicorn

## Install dependencies (+ Playwright chromium for JS-rendered template sites)
setup:
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m playwright install chromium

## Run the full unit suite (must stay green: 271 passed)
test:
	$(PYTHON) -m pytest tests -q

## Self-built static checker (64 files / 0 issues)
static:
	$(PYTHON) tools/static_check.py

## Markdown relative-link checker (README / docs screenshots & doc links)
links:
	$(PYTHON) tools/check_links.py

## Offline golden evaluation (P/R/F1 + section recall, machine-readable)
golden:
	$(PYTHON) tools/golden_check.py --offline --json

## Start the FastAPI service (submit / progress / results) on :8000
web:
	$(UVICORN) api.server:app --host 0.0.0.0 --port 8000

## Launch the tkinter desktop GUI
gui:
	$(PYTHON) site_crawler_gui.py

## Distributed scheduler: enqueue urls.txt then run 2 workers
scheduler:
	$(PYTHON) distributed/scheduler.py enqueue urls.txt
	$(PYTHON) distributed/scheduler.py run-workers --workers 2

## Everything the CI gate runs locally
check: test static links golden

## Run the pre-commit hooks on all files (needs: pip install pre-commit)
precommit:
	$(PYTHON) -m pre_commit run --all-files

## Remove Python caches and artifacts
clean:
	$(PYTHON) -c "import pathlib,shutil,os;\
root=pathlib.Path('.');\
[p.unlink() for p in root.rglob('*.pyc')];\
[shutil.rmtree(p) for p in root.rglob('__pycache__')];\
[shutil.rmtree(p) for p in root.rglob('.pytest_cache') if p.is_dir()];\
print('cleaned')"
