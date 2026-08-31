# LangGraph Multi-Agent Crawler Documentation (English)

Welcome to the project documentation. LangGraph Multi-Agent Crawler is a deterministic-first multi-agent web harvester: 9 specialist agents orchestrated by a LangGraph supervisor, where the LLM is an enhancer — never a dependency.

> [简体中文文档](https://github.com/LingMi1/langgraph-multi-agent-crawler/tree/main/docs/zh-CN)

## Getting started

- [Installation](installation.md) — dependencies, Playwright, Docker, environment & config
- [Architecture](architecture.md) — the supervisor graph, 9 agents, the degradation chain, storage & memory
- [API reference](api.md) — FastAPI service endpoints: crawl, results, SSE, history, results export
- [Development](development.md) — project layout, testing, static checks, golden eval, CI

## Quick links

- Project README: [English](../../README.md) · [简体中文](../../README.zh-CN.md)
- Security policy: [English](../../SECURITY.md) · [简体中文](../../SECURITY.zh-CN.md)
- Contributing: [English](../../CONTRIBUTING.md) · [简体中文](../../CONTRIBUTING.zh-CN.md)
- Code of conduct: [English](../../CODE_OF_CONDUCT.md) · [简体中文](../../CODE_OF_CONDUCT.zh-CN.md)
- Changelog: [CHANGELOG.md](../../CHANGELOG.md)

## Why this project exists

Most "AI crawlers" are thin wrappers that call an LLM on every page — slow, expensive, and they stall the moment the model endpoint hiccups. This project answers the production questions instead:

- How do you keep a 500-page crawl down to **3 LLM calls**?
- How do you survive an LLM endpoint outage *mid-run* without losing the crawl?
- How do you measure "did this change help" instead of guessing?
- How do you take over autonomously when deterministic extraction fails?

Read [architecture.md](architecture.md) for how the design answers each one.
