# 系统架构

本项目的核心思想是**确定性优先**：每个节点的默认动作都不依赖模型就能跑，LLM 只在判定点、受信号量限制地介入。任何一次 LLM 失败都会降级回确定性路径。采集才是产品，LLM 只是增强。

## 监督者图

`graph/workflow.py` 组装一个 LangGraph `StateGraph`，带条件路由。线性主路径：

```
START → Scout → Navigate → FetchExtract → Evaluate → Media → Storage → END
                              │  ▲          │  ▲
                              │  │(BFS循环)  │  │(审查裁决)
                              └──┘          │  │
                              └───────────► ConfigAdjust ◄┘
                                            CodeGen (LLM 最后保底)
                                            ReactTakeover (深度降级)
```

## 9 个 Agent

全部在 `graph/agents.py`，统一继承一个 `BaseAgent` 模板方法——轨迹记录、异常隔离、耗时统计都是免费的。

| Agent | 职责 | LLM |
|---|---|---|
| ScoutAgent 侦察兵 | 分析种子站点 → 站点画像 + 初始计划 | 否 |
| NavigateAgent 领航员 | 提取导航 → 填 BFS 队列 + 栏目清单 | 分类时可选 |
| FetchExtractAgent 执行者 | 抓取 + 规则清洗 + 落盘（确定性优先） | 深降级时 |
| EvaluateAgent 审查者 | 质量评估 + 对照计划检查完成度，裁决下一步 | 是（启发式护栏） |
| ConfigAdjustAgent 调整者 | 按评估建议调整配置重抓（≤3 次） | 否 |
| CodeGenAgent 规则生成者 | LLM 生成站点定制 CSS 选择器规则 | 是 |
| ReactTakeoverAgent 接管者 | 确定性链路全失败后 ReAct 自主接管 | 是 |
| MediaProcessorAgent 媒体处理者 | 图片过滤（装饰/二维码）+ 外链化 + 全局去重 | 否 |
| StorageAgent 存储者 | CSV 落盘 + 站点学习模式写入 | 否 |

## 降级链

`route_after_evaluate`（`graph/workflow.py`）是唯一的裁决闸口：

1. **评估通过** → 媒体处理 → 落盘。结束。
2. **不达标且调整次数 < 3** → `config_adjust`：改 UA / JS 渲染 / 请求头，清队列重爬。
3. **3 次仍败** → `code_gen`：LLM 生成站点定制 CSS 选择器规则，先校验再用。
4. **仍然失败** → `react_takeover`：ReAct 循环用白名单工具集决策 `retry` / `giveup`——即使确定性路径和生成路径全挂，系统也能带着已保存的内容收尾。

每一级都比上一级更难进、更昂贵才被需要；链条保证了采集必然以内容入库收尾，绝不丢页。

## 可靠性原语

- **运行级熔断**（`agents/breaker.py`）——两个 LLM 入口共用一个熔断器：连续 3 次重试耗尽的失败 → 本 run 快速失败后续所有调用；单次成功清零；run 启动复位。**熔断只禁 LLM，不禁采集。**
- **批量后置抢救**——BFS 热路径零 LLM（选择器定位吃 SQLite 持久化缓存）；正文不达标页进抢救队列，按 URL 模板分组，**每组只调一次 LLM** 定位选择器、泛化整个栏目。
- **多 provider 故障转移**（`agents/llm_pipeline.py`）——`chat_json`/`chat_stream` 在主 provider 重试耗尽后切到备用地址。
- **预算触发压缩**（`agents/react.py`）——超预算的对话历史折叠成一条摘要；LLM 摘要优先、规则摘要兜底。
- **离线 Golden 评估**（`tools/golden_check.py`）——3 个模板站，P/R/F1 用保守口径，`--json` 输出；回归可设 CI 门禁。

## 存储、记忆与安全

- **CSV + 文件**：每次运行写 `output/<域名>/crawl_results.csv` 和按栏目层级组织的页面 HTML（见 `agents/storage.py`）。
- **去重**：内容指纹（正文+标题的 MD5）保证相同页面只存一份；重复 CSV 行带 `<!-- duplicate … -->` 占位符，导出时排除。
- **站点记忆**（`memory.py`）：`site_patterns` 持久化站点类型 / JS 渲染 / 模板提示；同一站点第二次采集直接跳过重新侦察。
- **安全**（`agents/safety.py`）：不可信 HTML 进入 prompt 前先包裹成"数据而非指令"；严格 Pydantic 输出 schema；冲突时降权与确定性提取矛盾的 LLM 裁决。
- **合规**（`agents/fetcher.py`）：robots.txt 默认开启（标准库 `robotparser`）、TLS 校验默认开启、自限频率。

## 相关文档

- [安装指南](installation.md)
- [API 参考](api.md)
- [开发指南](development.md)
