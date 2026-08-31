# 安装指南

## 环境要求

- Python 3.11+
- Playwright + Chromium——**只有**需要 JS 渲染的模板站才用；其余全部走纯 `httpx`/`requests`
- Docker——可选，跑 FastAPI 服务镜像用

## 1. 安装依赖

```bash
pip install -r requirements.txt

# 浏览器引擎（仅 JS 渲染模板站需要）
playwright install chromium
```

## 2. 配置

复制环境模板——所有变量都可选，不设就自然走确定性降级路径：

```bash
cp .env.example .env
```

常用设置（`config.py` / `.env`）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `CRAWLER_API_KEY` | 不设（本地开发放开） | 设置后所有 API 端点要求 `X-API-Key` |
| `CRAWLER_RESPECT_ROBOTS` | `true` | robots.txt 合规（只遵守 `User-agent: *` 通配段） |
| `CRAWLER_TLS_VERIFY` | `true` | 出站爬取请求的 TLS 证书校验 |
| `LLM_BASE_URL` / `LLM_API_KEY` | 不设 | 主 LLM provider（DeepSeek 兼容的 OpenAI 端点） |
| `LLM_BACKUP_BASE_URLS` | 不设 | 备用 provider，主 provider 重试耗尽后切换 |
| `CRAWLER_CONCURRENCY` | 5 | 每次采集的抓取信号量大小 |

## 3. 运行

### CLI 采集

```bash
python -c "import asyncio; from graph.workflow import run_crawler; asyncio.run(run_crawler('https://example.com', max_steps=3000))"
```

### 桌面图形界面

```bash
python site_crawler_gui.py
```

### FastAPI 服务

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

### 分布式调度（SQLite 队列 + 多进程 worker）

```bash
python distributed/scheduler.py enqueue urls.txt
python distributed/scheduler.py run-workers --workers 2
python distributed/scheduler.py status
```

## 4. Docker

构建并运行 API 镜像（注意：Playwright/系统 Chrome **没有**打进镜像——JS 渲染站会走 httpx 降级路径；纯静态/BS4 站完整可用）：

```bash
docker build -t crawler-api .
docker run -p 8000:8000 -e CRAWLER_API_KEY=secret -v crawler_out:/app/output crawler-api
```

## 验证安装

```bash
python -m pytest tests -q            # 288 passed
python tools/static_check.py         # 64 个文件 / 0 问题
python tools/golden_check.py --offline --json   # P/R/F1 报告
```

完整的开发流程见 [开发指南](development.md)。
