# LangGraph 多智能体网页采集器 文档（简体中文）

欢迎。LangGraph 多智能体网页采集器是一个确定性优先的多 Agent 网页采集器：9 个专职 Agent 由 LangGraph 监督者编排，LLM 只是增强项——绝不是依赖项。

> [English Docs](https://github.com/LingMi1/langgraph-multi-agent-crawler/tree/main/docs/en)

## 快速开始

- [安装指南](installation.md) — 依赖、Playwright、Docker、环境变量与配置
- [系统架构](architecture.md) — 监督者图、9 个 Agent、降级链、存储与记忆
- [API 参考](api.md) — FastAPI 服务端点：提交、结果、SSE、历史、结果导出
- [开发指南](development.md) — 目录结构、测试、静态检查、Golden 评估、CI

## 快速链接

- 项目 README：[简体中文](../../README.zh-CN.md) · [English](../../README.md)
- 安全策略：[简体中文](../../SECURITY.zh-CN.md) · [English](../../SECURITY.md)
- 贡献指南：[简体中文](../../CONTRIBUTING.zh-CN.md) · [English](../../CONTRIBUTING.md)
- 行为准则：[简体中文](../../CODE_OF_CONDUCT.zh-CN.md) · [English](../../CODE_OF_CONDUCT.md)
- 更新日志：[CHANGELOG.md](../../CHANGELOG.md)

## 为什么做这个

市面上多数"AI 爬虫"是薄封装——每页都调 LLM，又慢又贵，模型端点一抖整站就停。这个项目回答的是生产问题：

- 怎么让 500 页的采集只调 **3 次 LLM**？
- 模型端点在**运行中途**挂了，怎么不丢采集？
- 怎么把"这次改动有没有变好"从感觉变成数字？
- 确定性提取全失败时，系统怎么自主接管？

答案都在 [architecture.md](architecture.md)。
