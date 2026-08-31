# 开发指南

## 目录结构

```
├── agents/          # 能力 Agent + 安全层 + 工具注册表 + 预算
│                    #   + react（FC 循环）+ semdedup + vector_retriever + eval
├── graph/           # 编排：workflow（监督者）/ agents（9 个）/ nodes / state
│                    #   + react_takeover（深度降级 ReAct 接管）
├── tests/           # 288 个单元测试（安全 / 计划 / 工具 / ReAct / 预算 /
│                    #   去重 / RAG / 评估指标 / FC 评估路径 / 图冒烟 /
│                    #   Golden 循环 / 回归 / 接管 / 熔断+抢救 /
│                    #   MCP / API / 分布式队列）
├── api/             # FastAPI 服务（server.py：提交/进度/结果 + SSE）
├── distributed/     # SQLite 任务队列 + 多进程调度器
├── tools/           # golden_check / compare_runs / static_check / rag_demo /
│                    #   mcp_server + mcp_client / gen_metrics_report
├── reports/         # metrics_report.{md,json} — 离线量化证据
├── memory.py        # SQLite 长期记忆（visited_urls / site_patterns）
├── schemas.py       # Pydantic 模型 + 日志
├── .github/         # CI（多版本 Python：pytest + 静态检查 + Golden）
└── main.py          # CLI 入口
```

## 三个壳、一个核心

`run_langgraph_crawler`（`graph/workflow.py`）是唯一的流水线入口。CLI（`main.py`）、tkinter 图形界面（`site_crawler_gui.py`）、FastAPI 服务（`api/server.py`）都是薄适配层。改流水线要改 `graph/`，而不是某个壳。

## 本地工作流

```bash
pip install -r requirements.txt

# 跑一次采集（一行命令）
python -c "import asyncio; from graph.workflow import run_crawler; asyncio.run(run_crawler('https://example.com', max_steps=3000))"

# 单元测试——必须保持全绿
python -m pytest tests -q            # 288 passed

# 自建静态检查（CI 里另有 ruff 交叉验证）
python tools/static_check.py         # 64 个文件 / 0 问题

# 离线 Golden 评估
python tools/golden_check.py --offline --json

# 回归对比（主指标变差 → 退出码 1）
python tools/compare_runs.py baseline.json current.json
```

也可以用 Makefile：`make test`、`make static`、`make golden`、`make check`（三个一起）、`make web`、`make gui`、`make clean`。

## 测试约定

- 每个行为变更都要带或更新测试。重点覆盖**降级路径**——项目的核心承诺是"LLM 挂了也能跑"。
- 测试绝不碰网络：抓取是注入/mock 的；Golden 离线模式读夹具。
- 新功能开自己的 `tests/test_*.py`，除非已有自然归属。

## Golden 评估

`tools/golden_check.py` 跑 3 个模板站（门户 / 电商 `books.toscrape` / 分页列表 `quotes.toscrape`），用保守口径 `overlap = min(saved, expected)` 出 P/R/F1 + 栏目发现率。`--json` 输出给 `tools/compare_runs.py` 做回归对比。

Golden 集是**回归的真相源**——不许"调到通过"。任何流水线改动都要重跑并保持数字稳定或更好。

## CI

`.github/workflows/ci.yml` 每次推送都跑：pytest（含覆盖率）、双重静态检查（`tools/static_check.py` + ruff）、Golden 验证、建议性 mypy。环境故意很严；推送前本地先 `make check`。

## 贡献

PR 约定与设计约束见 [CONTRIBUTING.zh-CN.md](../../CONTRIBUTING.zh-CN.md)（或 [CONTRIBUTING.md](../../CONTRIBUTING.md)）。一句话版：确定性优先、熔断保护的是采集不是 LLM、不加重量级依赖、conventional commit、PR 往 `main`。
