# 参与 LangGraph 多智能体网页采集器开发

> [English](CONTRIBUTING.md)

感谢你有兴趣参与。LangGraph 多智能体网页采集器是一个确定性优先的多 Agent 网页采集器：由 LangGraph 监督者编排 9 个专职 Agent，LLM 只是**增强项、绝不是依赖项**。下面的内容假设你已经把项目跑起来、并且想负责任地改动它。

## 环境准备

- **Python** 3.11+
- **Playwright** + Chromium（只在处理需要 JS 渲染的模板站时才用）
- **Docker**（可选——跑 FastAPI 服务和分布式调度器用；纯本地 CLI 开发不需要）

## 本地开发

```bash
git clone https://github.com/LingMi1/langgraph-multi-agent-crawler.git
cd langgraph-multi-agent-crawler

# 装依赖
pip install -r requirements.txt

# 浏览器引擎（只有要测 JS 渲染模板站才需要）
playwright install chromium

# 复制环境模板——API key 全部可选，缺了也会优雅降级
cp .env.example .env
```

三个壳、一个核心：CLI、tkinter 图形界面、FastAPI 服务共用同一个 `run_langgraph_crawler` 入口——改流水线一处，三处同时生效。所以优先改 `graph/`，不要只改某个壳。

## 跑起来

```bash
# CLI 采集（一行命令）
python -c "import asyncio; from graph.workflow import run_crawler; asyncio.run(run_crawler('https://example.com', max_steps=3000))"

# 桌面图形界面（TXT 批量导入 + 配置 + 逐站采集）
python site_crawler_gui.py

# REST 服务（提交 / 进度 / 结果）
uvicorn api.server:app --host 0.0.0.0 --port 8000

# 分布式调度（SQLite 队列 + 多进程 worker）
python distributed/scheduler.py enqueue urls.txt
python distributed/scheduler.py run-workers --workers 2
```

## 测试

```bash
# 全量单元测试——必须保持全绿
python -m pytest tests -q            # 288 passed

# 自建静态检查（64 个文件 / 0 问题）
python tools/static_check.py

# 离线 Golden 评估（P/R/F1 + 栏目发现率，机器可读）
python tools/golden_check.py --offline --json

# 回归对比（主指标变差 → 退出码 1）
python tools/compare_runs.py baseline.json current.json
```

行为变更如果已有对应测试文件，就扩展它；否则新增 `tests/test_*.py`。Golden 数字必须诚实：任何动了抓取/清洗/提取流水线的改动都要重跑 `tools/golden_check.py`，**不许**为了通过而"调"黄金集——它是回归的真相源。

## 代码检查

```bash
python tools/static_check.py         # 自建检查器（CI 里另有 ruff 交叉验证）
ruff check .                         # 本地装了 ruff 的话也可以跑
```

## 提交 PR 的约定

- 往 `main` 分支提 PR。
- 提交信息带 **conventional commit** 前缀（`feat:`、`fix:`、`refactor:`、`test:`、`docs:`、`chore:`、`revert:`）——正文中英文皆可。
- 一个 PR 只做一件事。
- 有行为变更就要补或改测试；提 PR 前跑一遍全量测试。

## CI 门禁

每个 PR 合入前必须通过 `.github/workflows/ci.yml` 里的全部关卡：`pytest`（含覆盖率）、双重静态检查（`tools/static_check.py` + ruff）、Golden 验证，以及（建议性）mypy。本地先把 `python -m pytest tests -q` 和 `python tools/static_check.py` 跑绿，能少踩很多坑——CI 环境是故意设得很严的。

## 值得遵守的设计约束

- **确定性优先。** 每个节点的默认路径必须不依赖 LLM 就能跑；模型只允许在判定点、受信号量限制地介入。任何把 LLM 调用变成 BFS 热路径刚需的改动都会被拒。
- **熔断保护的是采集，不是 LLM。** 两个 LLM 入口共用一个运行级熔断器；调用方必须降级，绝不能阻塞等待。
- **不加重量级依赖。** 项目刻意只用标准库 + 一小组固定依赖；能用标准库解决的（SQLite、`robotparser`、csv）就用标准库，除非有明确需求。
