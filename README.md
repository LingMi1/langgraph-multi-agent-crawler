# boyushixi — 多 Agent 智能网页采集器

一个基于 **LangGraph Supervisor 多智能体架构** 的企业级网页采集系统：对任意网站做
**侦察 → 导航 → 抓取清洗 → 质量评估 → 媒体处理 → 落盘** 的全流程自动化，LLM 仅在
关键决策节点介入（成本分层），全程可观测、可复现。

> 面试亮点叙事：Supervisor 模式 + Plan-and-Execute + 提示注入防护 + 经验记忆 +
> 全轨迹可观测 + 轻量 RAG（语义去重 + 向量检索 + Recall@k/NDCG@k 指标）+ Function Calling 闭环
> （含质量评估核心链路）+ LLM-as-judge 评估 + **离线 Golden 指标闭环（P/R/F1 + 栏目发现率 +
> 全指标回归对比）+ 双层静态检查（自研 stdlib 版 + ruff 交叉验证）** + **深降级 ReAct 自主接管
> （行动工具 + 多轮推理，确定性链路全失败后的兜底）** + **LLM 运行级熔断
> 与批量后置抢救（BFS 热路径零 LLM）** + 198 项单元测试 + **离线校招证据报告
> （`reports/campus_report.md`，6 个真实站点 240 页落盘 + 评估循环量化）**。
> 传统爬虫确定性为主、LLM 为辅，真实业务问题驱动。

---

## 1. 系统架构

```
                        ┌──────────────────────────────┐
                        │  Supervisor (graph/workflow) │  监督者：编排 + 条件路由
                        └──────────────┬───────────────┘
                                       │
     START → Scout ─→ Navigate ─→ FetchExtract ─→ Evaluate ─→ Media ─→ Storage → END
               │           │            │  ▲          │  ▲
               │           │            │  │(BFS循环)  │  │(审查裁决)
               │           │            └──┘          │  │
               │           └──────────► ConfigAdjust ◄─┘  │
               └──────────► (记忆命中)                     │
                                  CodeGen (LLM 最后保底) ──┘
```

**9 个编排级 Agent**（`graph/agents.py`，统一继承 `BaseAgent` 模板方法）：

| Agent | 职责 | LLM |
|-------|------|-----|
| ScoutAgent 侦察兵 | 分析种子站点 → 站点画像 + 初始任务计划(plan) | 否 |
| NavigateAgent 领航员 | 提取导航 → 填 BFS 队列 + 栏目清单并入计划 | 分类时可选 |
| FetchExtractAgent 执行者 | 抓取 + 规则清洗 + 落盘（确定性优先） | 深降级时 |
| EvaluateAgent 审查者 | 质量评估 + 对照计划检查完成度，裁决下一步 | 是（启发式护栏） |
| ConfigAdjustAgent 调整者 | 按评估建议调整配置重抓（≤3 次） | 否 |
| CodeGenAgent 规则生成者 | LLM 生成站点定制清洗规则（只产出 CSS 选择器） | 是 |
| ReactTakeoverAgent 接管者 | 确定性链路全失败后 ReAct 自主接管（行动工具 + 多轮推理） | 是 |
| MediaProcessorAgent 媒体处理者 | 图片过滤（装饰/二维码）+ 外链化 + 全局去重 | 否 |
| StorageAgent 存储者 | CSV 落盘 + 站点学习模式写入 | 否 |

## 2. 技术栈

- **语言/运行时**：Python 3.11 · asyncio 并发（信号量限流）
- **多智能体编排**：LangGraph（StateGraph + TypedDict State + Reducer）
- **LLM**：langchain-openai（内网 DeepSeek 兼容端点）
- **抓取**：httpx / requests · 反爬 Stealth · Playwright（JS 模板站渲染补链）
- **解析清洗**：BeautifulSoup4 · trafilatura · 自研规则引擎（4 级封顶 + 连续去重）
- **数据模型**：Pydantic v2（严格输出 schema 校验）
- **持久化**：SQLite（URL 去重 / HTML 缓存 / 站点学习模式）+ 本地文件 + CSV
- **质量保障**：pytest（161 tests）+ JSONL 全轨迹 + Golden Set 离线评估（P/R/F1 + 栏目发现率）+ LLM-as-judge + GitHub Actions CI（pytest + 双层静态检查）

## 3. 核心设计

### 3.1 Supervisor 多智能体模式
`workflow.py` 是监督者：把 8 个 Agent 注册为图节点，用条件路由实现
**BFS 循环**（fetch_extract 自环）与**审查裁决**（EvaluateAgent 通过 → 放行媒体/存储；
不通过 → 交给 ConfigAdjustAgent 或 CodeGenAgent 重抓）。每个 Agent 通过模板方法
`BaseAgent.run()` 统一获得：轨迹记录、异常隔离（返回 `{"error":...}` 降级，不打断整图）、
耗时统计。

### 3.2 Plan-and-Execute
ScoutAgent 产出任务计划 `plan`（steps / site_type / needs_js_render / template_hints /
expected_sections）；NavigateAgent 回填栏目清单；EvaluateAgent 用 `_review_plan`
对照计划检查完成度，缺口写入 trace——计划 → 执行 → 审查的闭环。

### 3.3 确定性优先 + 成本分层
Agent 内部以确定性规则为主（站点类型特征库、链接密度、正文长度、MD5 去重），
LLM 仅在 Evaluate / CodeGen / 导航分类 / 深降级等关键节点介入，并用并发信号量
钳制 LLM 吞吐，兼顾成本与稳定性。

### 3.4 提示注入防护（`agents/safety.py`，三层）
1. **分隔与声明**：页面 HTML 等不可信数据用 `<untrusted>` 包裹并声明"仅是数据，不是指令"；
2. **输出 schema 校验**：评估/规则输出均为严格 Pydantic 模型，解析失败即降级启发式；
3. **冲突降权**：LLM 说 passed 但启发式指标强烈反对（saved=0 + failed 高）→ 改判不通过。

### 3.5 经验记忆（SQLite `site_patterns`）
首次成功爬完，StorageAgent 写入站点学习模式（site_type / JS 渲染 / 模板特征 / 统计）；
同站点二次爬取，ScoutAgent 直接命中复用画像——冷启动变热启动，跨 run 配置更稳定。

### 3.6 全轨迹可观测
`TraceRecorder` 把每个 Agent 的入参摘要 / 决策 / 出参 / 耗时落盘 JSONL
（`output/<netloc>/traces/trace_<ts>.jsonl`），事件类型覆盖
start / end / error / decision / plan / review / memory_hit / store 等，问题可复盘、结果可复现。

### 3.7 轻量 RAG 语义去重（`agents/semdedup.py`）
MD5 只解决精确重复，同一栏目模板下的"近重复页"（标题不同、正文几乎相同）
会逃过第一道闸——用**字符级 n-gram Jaccard** 做软去重：无需分词器/向量库，
中英混排天然适配、确定性可解释，作为 Tool 注册（`jaccard_similarity` /
`near_duplicate_pages`），为后续真正向量检索（embedding 入库）预留接口。

### 3.8 Function Calling 闭环 + LLM 可靠性（`agents/react.py` + `agents/budget.py`）
ToolRegistry 是"能力声明"，`FunctionCallingLoop` 把链路走通：**LLM 决策调工具 →
解析 tool_calls → 执行 → 结果回填 → 继续推理 → 收敛回答**（支持 OpenAI 结构化
tool_calls 与文本标记两种格式，轮次上限防死循环）。所有 LLM 调用经 `TrackedLLM`
统一**重试 + 指数退避 + token 记账**——调用层负责扛模型不稳定，业务代码零改动。

**Tool-Use 安全三件套**（模型的输出不可信，安全边界在工具执行层）：
1. **参数净化** `sanitize_tool_args`：按工具 JSON Schema 剥离未知 key（防注入参数）、
   字符串截断到上限、类型强制（数值解析失败即丢弃）；
2. **FC 审计轨迹**：每轮落 trace 记录 `args_preview`（模型原始请求）与
   `sanitized_preview`（净化后实际执行）及出参摘要，失败同样留痕，可复核；
3. **未知工具显式拒绝**：未注册/恶意工具名直接拒绝并审计（`status=unknown_tool`），
   不打断循环、不落执行。

### 3.9 多 Agent 协作模式：为什么选 Supervisor
面试高频题"多 Agent 怎么协作"的选型对照（本项目=单层 Supervisor）：

| 模式 | 思路 | 优点 | 缺点 | 适用 |
|------|------|------|------|------|
| **Supervisor（本）** | 监督者统一编排，条件路由分派 | 流程可控、失败可降级、状态集中 | 单点（监督者）、消息路径长 | 流程固定、阶段强依赖的任务 |
| Handoff | Agent 间直接转交控制权 | 灵活、低延迟 | 难以追踪、易乒乓/死循环 | 客服对话类 |
| 黑板(Blackboard) | 共享状态，Agent 各自读写 | 松耦合、并行 | 读写竞争、收敛难保证 | 认知任务、多解问题 |
| 平级广播 | 事件总线，各 Agent 订阅 | 高扩展 | 顺序不确定、难复现 | 无严格顺序的批处理 |

**本项目选型理由**：采集是"侦察 → 导航 → 抓取 → 评估 → 存储"的强顺序流水线，
阶段间有严格依赖（先有 plan 才有导航、先评估通过才落盘），Supervisor 的单点正是
它的优点——异常可以隔离在单个 Agent 内并降级，整图不中断。若未来要做多站点并行
采集，可把 Supervisor 升级为"每站点一个 sub-graph + 顶层协调器"的分层模式。

### 3.9.1 节点职责 vs 框架能力（面试追问防线："LangGraph 帮你写了多少？"）
先亮底线：**StateGraph 只给了"节点注册 + 条件路由 + recursion_limit + 状态合并"**——
路由到哪个节点的**判断逻辑**（`route_after_evaluate` 的 passed/adjustment/generation/react 四级裁决）、
每个节点**失败时的独立降级路径**、以及 9 个节点各自的业务逻辑，全部是本项目写的。
下表每行回答面试官的同一组问题：*"这个节点失败会发生什么？LLM 在哪介入？最终兜底是什么？"*

| 节点 | 失败信号 | 默认动作（确定性，LLM 不在场也能跑） | LLM 介入点（全部可降级回默认） | 最终兜底 |
|------|----------|--------------------------------------|-------------------------------|----------|
| scout | `analyze()` 异常 | PageScout 规则判定 site_type/JS 需求 + plan 推导 + 注入检测 | 无直接调用（经验记忆修正画像） | error → 路由到 storage 收尾 |
| navigate | 首页抓取失败/HTML<100 | NavigationParser 提取导航 + URL→nav_path 注册表登记 | 无（JS 菜单 Playwright 硬提取是工具降级） | 保留静态映射；error → storage |
| fetch_extract | 抓取异常/反爬拦截/清洗失败 | 并发抓取 + 反爬检测 + 图片抢救5层 + 列表/详情分支 + 模板落盘 | 正文定位（模板缓存复用）/导航分类（URL前缀缓存）/自适应整篇清洗（默认 off） | 每级 LLM 失败都回退确定性：selector→代码启发式、分类→规则 nav_path、清洗→原正文 |
| evaluate | LLM 调用异常/输出不可解析 | 启发式打分（saved/failure_rate 规则） | FC 工具裁决（`quality_judge`）→ 纯文本 → 启发式，`guard_llm_verdict` 冲突降权 | 启发式 + 护栏否决 LLM 通过 |
| config_adjust | 无评估结果 | 应用 recommended_ua/js/headers → 重建队列 → 计数 | 无（消费上轮评估结论） | error → storage |
| code_gen | 无样本/无 LLM/校验失败 | — | LLM 生成 CSS 选择器规则 | `_validate_rules` 白名单（禁 javascript:/exec 等）；失败置 `generation_attempted` 不再重试 |
| react | 无 LLM/已触发/解析失败 | — | `FunctionCallingLoop` 行动工具 `fetch_page`/`apply_config` | giveup → 保守落盘；`react_attempted` 一次性触发 |
| media_processor | 单页处理异常 | 图标/二维码过滤 + 外链保留 + 防盗链 403 降级链 + 文件重写 | 失败图片 LLM 替代方案（可选） | 单页跳过，不炸整图 |
| storage | 无结果 | URL 去重 + CSV 12 字段 + html 瘦身 | 无 | 磁盘扫描重建 `_rebuild_csv_from_disk`（不丢数据） |

一句话叙事：*"框架提供的是路由骨架，降级策略是我们设计的——每个节点失败都有一条不依赖 LLM 的确定性出路，LLM 只是在这条链上'增强'，永远不是'必须'。"*

### 3.10 RAG 检索链路（`agents/vector_retriever.py` + `tools/rag_demo.py`）
轻量 RAG 从"去重工具"升级为**完整检索链路**：爬取落盘 HTML → 抽取正文 →
**字符 n-gram TF-IDF 稀疏向量 + 余弦相似度**建索引 → `rag_search` 工具语义检索。
纯 Python 零外部依赖（不依赖 numpy/sklearn/embedding），离线可跑、原理可讲：
TF-IDF 是 BM25 前的经典基线，稀疏向量换 numpy/向量库即可上量。演示：
`python tools/rag_demo.py hnbn666`（建索引 + 语义查询 + 注册成 Agent 可调工具）。
检索质量可量化：**Recall@k / NDCG@k**（`agents/eval.py`，位置感知增益），
配合 golden 相关文档集合即可评估"检索是否命中正确栏目"，而不只是"能搜到"。

### 3.11 Agent 评估体系（`agents/eval.py` + `tools/golden_check.py` + `tools/compare_runs.py`）
离线评估"三件套"：**P/R/F1 + Recall@k/NDCG@k 指标**（召回/检索任务的量化口径）→
**LLM-as-judge**（规则覆盖不了的质量维度，输出强制 JSON `{score, reason}`，
可插拔可审计）→ **回归对比**。让"改动是好是坏"从感觉变成数字。

**Golden 指标闭环**（`tools/golden_check.py`）：对 3 个不同模板的公开演示站
（门户 / 电商 books.toscrape / 分页列表 quotes.toscrape）做离线评估，报告直接输出
**P/R/F1**（落盘覆盖率：`overlap = min(saved, expected)` 保守口径，不臆测未落盘页面的正确性）
与**栏目发现率**（`section_recall`：落盘目录顶层栏目名 与 expected_sections 的召回）——
验证"这次改动让 recall +0.2"，而不是只报"保存了 N 页"。支持 `--offline --json`
输出机器可读指标，供脚本消费。

**回归对比**（`tools/compare_runs.py`）：对比两次 golden 报告的**全指标 diff**
（saved / recall / f1 / section_recall / keyword_hit），任一主指标变差即判定
**REGRESSION**（退出码 1 供 CI 挂钩），任一变好且无变差判定 IMPROVED，全同判定
SAME——量化回归，不靠肉眼。

其中评估裁决走**核心链路 tool-calling**：`evaluate_node` 优先让 LLM 通过
`FunctionCallingLoop` 调用 `quality_judge` 工具（确定性打分：正文长度/链接密度/图片数），
拿到客观分后再输出评估 JSON（`eval_source=llm_fc` 可观测）；模型不支持工具标记、
输出不可解析或 LLM 抛异常时逐级降级到纯文本 LLM → 启发式，评估链路不因模型
不稳定而中断——把"伪多智能体"变成可实锤的 tool-calling 闭环。

### 3.12 深降级 ReAct 自主接管（`graph/react_takeover.py`，面试高频题"LLM 不可替代性"的答案）
三层兜底链：**确定性爬虫（默认执行者）→ LLM 评估/规则生成（关键节点介入）→ 自主接管（兜底）**。
当传统爬虫、配置调整（≤3 次）、LLM 生成规则全部失败后，`ReactTakeoverAgent` 进入 ReAct 模式
**自主诊断并决策**：LLM 通过 `FunctionCallingLoop` 调用**行动工具**——`fetch_page`（侦察式抓取，
确认可达性/空壳/反爬）+ `apply_config`（生成新抓取配置，白名单字段）——多轮推理后收敛为
`retry`（改配置重抓）或 `giveup`（保守落盘）。

与评估节点 FC 路径的分工：`quality_judge` 是**只读分析**工具（拿客观分再裁决），
`fetch_page`/`apply_config` 是**行动**工具（Agent 真正动手）——从"能判断"到"能行动"。

护栏：`react_attempted` 一次性触发（绝无第二轮，防死循环）、`max_rounds=4` 成本兜底、
无 LLM/决策解析失败 → 保守 giveup、工具参数白名单净化——**深降级不能成为新的失控源**。

**真实链路已验证**（内网 DeepSeek 网关真爬 hnbn666）：evaluate `passed=False score=0.4` →
react_node 触发 → LLM 多轮行动后收敛 **`decision=retry`** → 新配置重抓 → 收敛落盘 6 页
（golden P/R/F1=1.0 保持）。`reports/campus_report.md` 的"接管"列即该轨迹证据。

### 3.13 校招证据报告（`tools/gen_campus_report.py` → `reports/`）
把"真的跑过"变成"能摆上桌的数字"（全部离线可复现）：对 golden 清单中已落盘的站点输出
**P/R/F1**；对 6 个真实站点输出**保存量/栏目/轨迹统计**（累计 240 页）；从 trace 提取
**评估循环证据**（调整前 vs 调整后：如 xnjzgc.cn 保存量 1→98、zztzmjg.com 3→84；
hnbn666 触发 ReAct 接管 `decision=retry`），
直接回答面试官的"你的指标是多少分 / 评估循环真的工作吗 / LLM 真的会动手吗"。

### 3.14 LLM 运行级熔断 + 批量后置抢救（`agents/breaker.py`，BFS 热路径零 LLM）
问题实锤：LLM 定位选择器曾挂在 BFS **每页同步热路径**——内网推理端点半死时单页重试超时
30–60s，trafilatura 秒清成功仍被拖死，86 页后整夜停滞。解法是两件套：

- **运行级熔断**：`LLMCircuitBreaker` 全局单例，**两个 LLM 入口统一接入**（`chat_json`
  AsyncOpenAI 直连 / `TrackedLLM` langchain 包装）——连续 3 次调用失败（重试耗尽口径，
  不放大内部单次重试）→ 本 run 熔断，后续调用**零等待快速失败**（调用方降级，不再等超时）；
  单次成功清零；run 启动复位（GUI 多站连跑不互相污染）；**熔断只禁 LLM 不禁爬取**。
- **批量后置抢救**：热路径零 LLM（`llm_locate` 只吃 SQLite 持久化缓存，未命中启发式兜底）；
  正文不达标页（非功能页/二维码页）进 `rescue_queue`，evaluate 阶段按 **URL 模板分组**
  （路径数字段→`{N}`），**每组只调一次 LLM 定位选择器、泛化整个栏目**；熔断打开/无 key/
  定位失败/预算溢出（`RESCUE_MAX_PAGES`/`RESCUE_MAX_TEMPLATES`）→ **降级保存+标记**
  （`rescued`/`rescue_degraded`/`rescue_skipped`/`rescue_dup`/`rescue_fetch_failed`
  统计口径），Fetcher 缓存零网络重取——内容已支付抓取成本，绝不因 LLM 不可用而丢弃。

**真实站点对比**（zztzmjg.com 同站实测）：旧代码 86 页停滞整夜（每页 LLM 超时 30s+）→
新代码 **85 秒全站跑完**（329 次抓取/88 页保存/失败 0）、LLM 调用降到 **3 次**、复跑仅
**1 次**（选择器持久化缓存命中后抢救阶段零 LLM）、批量抢救 4/4 成功（一次定位
`article.grid_8 .content` 泛化全组，101–131 字正文救回）。

## 4. 关键工程决策与踩坑

- **规则 12/13（RuiQiCMS 可视化建站）**：纯图产品详情页正文仅 12–16 字，
  会被"正文过短/纯图页"拦截误杀——用 `product_content_title` 容器 + `<img>` 双信号
  放行落盘（标题 + 产品图），并强制走 BS4 跳过 trafilatura（会剥掉标题容器）。
- **信号必须用清洗前 HTML**：`product_content_title` 在 extractor 执行后会被剥离，
  判定必须基于 `rescued_html` 在 extract 之前一次性计算、全程复用。
- **JS 模板站补链**：rzq 模板首页需 Playwright 渲染后才能发现详情链接，且渲染与
  静态抓取要做合并去重（并集 + 静态优先），避免重复渲染。
- **URL 归一化去重**：过滤 utm/token 追踪参数、折叠 `index.php` 路由变体、
  分页 URL 与 uuid-N 详情页区分，杜绝 `_N.html` 文件爆炸。
- **4KB 样本截断**：LLM 定位正文容器时压缩页面为 4KB 样本，控 token 且防提示注入面扩大。

## 5. STAR 量化成果

- **S**：中小型企业官网结构差异大（静态 / JS 模板 / 可视化建站），人工清洗效率低。
- **T**：一套系统自适应任意网站并自动化全流程。
- **A**：Supervisor 多智能体 + 计划执行审查闭环 + LLM 关键节点介入 + 规则引擎。
- **R**：6 个真实站点累计 240 页落盘（xnjzgc.cn 98 页 / zztzmjg.com 84 页）；
  198 项单元测试全绿；评估循环实锤：4 次运行触发 12 次配置调整，
  xnjzgc.cn 保存量 1→98、zztzmjg.com 3→84；hnbn666.cn golden 离线 P/R/F1=1.0；
  站点学习模式二次爬取 100% 命中；`reports/campus_report.md` 全部指标离线可复现。

## 6. 快速开始

```bash
pip install -r requirements.txt
playwright install chromium          # JS 模板站渲染用

# 命令行爬取
python -c "import asyncio; from graph.workflow import run_crawler; asyncio.run(run_crawler('https://example.com', max_steps=3000))"

# 单元测试
python -m pytest tests -q            # 198 passed

# 离线静态检查（自研 stdlib 版，等价 ruff F401/F403/F811/F821）
python tools/static_check.py         # 41 文件 / 0 问题

# Golden 离线评估（P/R/F1 + 栏目发现率，--json 机器可读）
python tools/golden_check.py --offline --json

# 校招证据报告（离线生成：golden 指标 + 实地站点统计 + 评估循环证据）
python tools/gen_campus_report.py    # → reports/campus_report.{md,json}

# 回归对比（全指标 diff，REGRESSION 退出码 1）
python tools/compare_runs.py baseline.json current.json

# RAG 检索链路演示（对落盘 HTML 建索引 + 语义查询）
python tools/rag_demo.py hnbn666
```

GUI 入口（校招版双栏工作台）：`python desktop_app.py`（pywebview/WebView2，
业务可替换壳层：`runner_base.py` 定义 `TaskRunner` 协议，切换业务零改动）；
旧版：`python site_crawler_gui.py`（Tkinter）。

## 7. 目录结构

```
├── agents/          # 能力级 Agent + safety 安全层 + base 抽象 + tools(工具注册)
│                    #   + budget(成本/重试) + react(FC闭环) + semdedup(去重)
│                    #   + vector_retriever(RAG检索) + eval(评估指标/LLM-judge)
├── graph/           # 编排级：workflow(Supervisor) / agents(9 Agent) / nodes(节点逻辑) / state(TypedDict)
│                    #   + react_takeover(深降级 ReAct 接管：行动工具 + 多轮推理)
├── tests/           # 198 项单元测试（safety / plan / BaseAgent / 工具 / 工具安全
│                    #   / ReAct / 记账 / 去重 / RAG检索 / 评估指标 / FC评估链路
│                    #   / 图装配冒烟 / golden 指标闭环 / 回归对比 / 深降级接管
│                    #   / LLM熔断 + 批量抢救）
├── tools/           # analyze_trace(轨迹分析) / golden_check(离线评估,支持--json/--offline)
│                    #   / compare_runs(全指标回归对比) / static_check(自研静态检查) / rag_demo
│                    #   / gen_campus_report(校招证据报告 → reports/)
├── reports/         # campus_report.{md,json} 离线量化证据
├── webui/           # 校招版前端（双主题 / 统计面板 / toast / 拖拽 / 动态表单）
├── runner_base.py   # TaskRunner 业务协议（换业务零改动的架构支点）
├── desktop_app.py   # pywebview 桌面壳（校招版 GUI 入口）
├── memory.py        # SQLite 长期记忆（visited_urls / site_patterns）
├── schemas.py       # Pydantic 模型 + 日志
├── .github/         # GitHub Actions CI（多版本 Python：pytest + 双层静态检查 + golden 校验）
├── main.py          # 命令行入口
└── site_crawler_gui.py  # Tkinter GUI（旧版）
```

## 8. English Summary / 外企面试叙事

**boyushixi** is a multi-agent web harvesting system built on LangGraph's
Supervisor pattern. A coordinator (workflow) orchestrates 9 specialist agents
(scout → navigate → fetch/extract → evaluate → media → storage) over a
plan-and-execute loop, using **deterministic rules first, LLM only at critical
decision points** (cost layering). When the deterministic chain, config
adjustments, and generated rules all fail, a **deep-degradation ReAct agent
takes over** — calling action tools (`fetch_page` / `apply_config`) across
multi-round reasoning to decide retry vs. graceful give-up (guarded by a
one-shot flag, a 4-round cap, and whitelist-sanitized tool args).

Engineering highlights: three-layer prompt-injection defense (isolation +
strict Pydantic output schema + heuristic veto), cross-run site-pattern memory
(cold→warm start), full JSONL trace observability, a tool layer with a
function-calling loop (retry + exponential backoff + token budgeting), and
lightweight RAG — n-gram Jaccard dedup plus a zero-dependency TF-IDF cosine
retriever over harvested pages (scored with Recall@k / NDCG@k) — plus a
quantitative eval suite (P/R/F1, LLM-as-judge, run-to-run regression diff in CI),
where the quality gate itself runs through a function-calling loop
(LLM invokes a deterministic `quality_judge` tool before emitting its verdict).
An offline golden-set harness scores 3 template sites (portal / ecommerce /
paged list) with P/R/F1 plus a section-recall metric, and a self-built
stdlib-only static checker (F401/F403/F811/F821, cross-verified by ruff in CI)
keeps the codebase clean. A `tools/gen_campus_report.py` regenerates offline
evidence (golden metrics + 6 real sites / 240 pages + evaluation-loop traces
showing saved 1→98 on one site) into `reports/`.
A run-level LLM circuit breaker (3 consecutive exhausted-retry failures →
fast-fail for the rest of the run) plus batched post-hoc rescue (zero-LLM hot
path, one selector-locate per URL-template group, degraded save when the LLM
is unavailable) took a real site from "stalled overnight at 86 pages" to
"full crawl in 85 s with 3 LLM calls".
198 unit tests.

**STAR template** (45–60s elevator pitch for English interviews):

> S: Small/medium business sites are wildly heterogeneous (static, JS-templated,
> WYSIWYG builders); manual cleaning doesn't scale.
> T: One system that adapts to any site and automates the whole pipeline.
> A: Supervisor multi-agent graph; plan → execute → review loop; LLM reserved
> for decisions that need judgment; a rule engine for deterministic extraction;
> memories, observability, and an offline golden-set eval to prove it.
> R: 6 real sites / 240 pages harvested (98 pages on one); 198 tests green in
> CI; evaluation loops fired 12 config adjustments across 4 runs (saved 1→98
> on one site); hnbn666.cn golden P/R/F1 = 1.0 offline; a run-level LLM
> circuit breaker + batched rescue cut one site from overnight stall to an
> 85-second full crawl; all numbers reproducible offline via
> `python tools/gen_campus_report.py`.
