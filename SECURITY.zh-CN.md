# 安全策略

boyushixi 是一个研究/面试向项目——多 Agent 网页采集器。它不是加固过的产品，但在面向不可信输入与网络的每个部分都遵循**默认安全、显式例外**原则。

[English](SECURITY.md)

## 受支持项 / 默认值

| 关注点 | 默认 | 覆盖方式 |
|--------|------|----------|
| 出站爬取请求的 TLS 证书校验 | **开启**（`CRAWLER_TLS_VERIFY=true`） | 仅内网自签证书站可设 `false` 显式豁免 |
| robots.txt 合规（只遵守 `User-agent: *` 通配段） | **开启**（`CRAWLER_RESPECT_ROBOTS=true`） | 仅当你明确拥有目标站点时设 `false` |
| API key 鉴权（`X-API-Key`） | 未设 `CRAWLER_API_KEY` 时放行（本地开发友好） | 设置 `CRAWLER_API_KEY` 即强制开启 |
| LLM 提示注入防御 | 三层，见下 | 不建议关闭 |

## 不可信输入如何处理

采集器从开放网络摄入**不可信的 HTML**，并把片段喂给 LLM。防御是分层的，关键处 fail-closed：

1. **分隔与声明** —— 不可信内容在进入任何 prompt 前由 `wrap_untrusted()` 包裹，带显式标签与长度上限，让模型能区分"数据"与"指令"。
2. **输出 schema 校验** —— 所有 LLM 输出（`EvaluationResult`、`ExtractionRules` 等）在使用前都经过严格 Pydantic schema 校验；校验失败即丢弃，绝不部分信任。
3. **冲突降权** —— 当 LLM 裁决与确定性提取器直接冲突时，`guard_llm_verdict()` 降权 LLM 意见，而不是静默采信。

任何检测到的注入尝试都会记录日志（见 `log_injection_warning`）。

## API 层

- 鉴权：用 `secrets.compare_digest`（恒定时间比较）比对 `X-API-Key` 与 `CRAWLER_API_KEY`；未设 key → 开放（本地开发），设了 key → 所有端点不匹配一律 401。
- 限流：60s 滑动窗口——每客户端最多 6 次提交、30 个 SSE 连接；限流主体优先取 `X-Forwarded-For`（反代后的真实 IP 首跳），否则退回 API key。
- 任务表有上限（`deque(maxlen=20)`）约束内存增长；每任务日志环形缓冲上限 4000 行。

## 出站爬取安全

- 除显式豁免外，每个请求都校验对端证书。
- 抓取 URL 前先查询目标源站 `robots.txt`（只遵守 `User-agent: *` 通配段）。robots 缺失 / 404 / 网络失败 → **放行**（绝不让合规检查拖垮抓取）。被禁路径返回 `blocked_by_robots`。
- robots 解析器按**源站**缓存（不是按 URL），保证 `Disallow: /private` 这类按路径规则的正确性。

## 报告漏洞

这是面试演示项目，不是生产服务。如果你仍发现了可利用的问题：

- **不要**在公开 issue 里贴利用细节。
- 向仓库所有者发一份简短报告（issues 页 → 私密说明）：受影响端点/模块、最小复现、影响范围。

## 明确的不在范围内

- 多租户隔离、针对任意内网目标的 SSRF 加固、密钥轮换、正式威胁建模均**不在本项目范围内**，在此明确说明以免误判。
