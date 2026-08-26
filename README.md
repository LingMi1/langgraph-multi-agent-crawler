# boyushixi — 多 Agent 智能网页采集器

一个基于 **LangGraph Supervisor 多智能体架构** 的企业级网页采集系统：对任意网站做
**侦察 → 导航 → 抓取清洗 → 质量评估 → 媒体处理 → 落盘** 的全流程自动化，LLM 仅在
关键决策节点介入（成本分层），全程可观测、可复现。

> 面试亮点叙事：Supervisor 模式 + Plan-and-Execute + 提示注入防护 + 经验记忆 +
> 全轨迹可观测 + 轻量 RAG（语义去重 + 向量检索）+ Function Calling 闭环 +
> LLM-as-judge 评估 + 117 项单元测试。传统爬虫确定性为主、LLM 为辅，真实业务问题驱动。

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

**8 个编排级 Agent**（`graph/agents.py`，统一继承 `BaseAgent` 模板方法）：

| Agent | 职责 | LLM |
|-------|------|-----|
| ScoutAgent 侦察兵 | 分析种子站点 → 站点画像 + 初始任务计划(plan) | 否 |
| NavigateAgent 领航员 | 提取导航 → 填 BFS 队列 + 栏目清单并入计划 | 分类时可选 |
| FetchExtractAgent 执行者 | 抓取 + 规则清洗 + 落盘（确定性优先） | 深降级时 |
| EvaluateAgent 审查者 | 质量评估 + 对照计划检查完成度，裁决下一步 | 是（启发式护栏） |
| ConfigAdjustAgent 调整者 | 按评估建议调整配置重抓（≤3 次） | 否 |
| CodeGenAgent 规则生成者 | LLM 生成站点定制清洗规则（只产出 CSS 选择器） | 是 |
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
- **质量保障**：pytest（117 tests）+ JSONL 全轨迹 + Golden Set 离线评估 + LLM-as-judge + GitHub Actions CI

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

### 3.10 RAG 检索链路（`agents/vector_retriever.py` + `tools/rag_demo.py`）
轻量 RAG 从"去重工具"升级为**完整检索链路**：爬取落盘 HTML → 抽取正文 →
**字符 n-gram TF-IDF 稀疏向量 + 余弦相似度**建索引 → `rag_search` 工具语义检索。
纯 Python 零外部依赖（不依赖 numpy/sklearn/embedding），离线可跑、原理可讲：
TF-IDF 是 BM25 前的经典基线，稀疏向量换 numpy/向量库即可上量。演示：
`python tools/rag_demo.py hnbn666`（建索引 + 语义查询 + 注册成 Agent 可调工具）。

### 3.11 Agent 评估体系（`agents/eval.py` + `tools/compare_runs.py`）
离线评估"三件套"：**P/R/F1 指标**（召回型任务的量化口径）→ **LLM-as-judge**
（规则覆盖不了的质量维度，输出强制 JSON `{score, reason}`，可插拔可审计）→
**回归对比**（`compare_runs.py` 对比两次 golden 报告的 saved/关键词/耗时，
REGRESSION 退出码 1 供 CI 挂钩）。让"改动是好是坏"从感觉变成数字。

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
- **R**：hnbn666.cn 全站冒烟 `saved=6 / failed=0`；117 项单元测试全绿；
  单次运行 26 条 trace 事件完整落盘；站点学习模式二次爬取 100% 命中；
  RAG 检索 demo 对落盘页面建索引并命中正确栏目。

## 6. 快速开始

```bash
pip install -r requirements.txt
playwright install chromium          # JS 模板站渲染用

# 命令行爬取
python -c "import asyncio; from graph.workflow import run_crawler; asyncio.run(run_crawler('https://example.com', max_steps=3000))"

# 单元测试
python -m pytest tests -q            # 117 passed

# RAG 检索链路演示（对落盘 HTML 建索引 + 语义查询）
python tools/rag_demo.py hnbn666
```

GUI 入口：`python site_crawler_gui.py`（博宇 · 网站爬取工具）。

## 7. 目录结构

```
├── agents/          # 能力级 Agent + safety 安全层 + base 抽象 + tools(工具注册)
│                    #   + budget(成本/重试) + react(FC闭环) + semdedup(去重)
│                    #   + vector_retriever(RAG检索) + eval(评估指标/LLM-judge)
├── graph/           # 编排级：workflow(Supervisor) / agents(8 Agent) / nodes(节点逻辑) / state(TypedDict)
├── tests/           # 117 项单元测试（safety / plan / BaseAgent / 工具 / ReAct / 记账
│                    #   / 去重 / RAG检索 / 评估 / 图装配冒烟）
├── tools/           # analyze_trace(轨迹分析) / golden_check(离线评估,支持--json)
│                    #   / compare_runs(回归对比) / rag_demo(RAG检索演示)
├── memory.py        # SQLite 长期记忆（visited_urls / site_patterns）
├── schemas.py       # Pydantic 模型 + 日志
├── .github/         # GitHub Actions CI（多版本 Python 跑 pytest）
├── main.py          # 命令行入口
└── site_crawler_gui.py  # Tkinter GUI
```

## 8. English Summary / 外企面试叙事

**boyushixi** is a multi-agent web harvesting system built on LangGraph's
Supervisor pattern. A coordinator (workflow) orchestrates 8 specialist agents
(scout → navigate → fetch/extract → evaluate → media → storage) over a
plan-and-execute loop, using **deterministic rules first, LLM only at critical
decision points** (cost layering).

Engineering highlights: three-layer prompt-injection defense (isolation +
strict Pydantic output schema + heuristic veto), cross-run site-pattern memory
(cold→warm start), full JSONL trace observability, a tool layer with a
function-calling loop (retry + exponential backoff + token budgeting), and
lightweight RAG — n-gram Jaccard dedup plus a zero-dependency TF-IDF cosine
retriever over harvested pages — plus a quantitative eval suite
(P/R/F1, LLM-as-judge, run-to-run regression diff in CI). 117 unit tests.

**STAR template** (45–60s elevator pitch for English interviews):

> S: Small/medium business sites are wildly heterogeneous (static, JS-templated,
> WYSIWYG builders); manual cleaning doesn't scale.
> T: One system that adapts to any site and automates the whole pipeline.
> A: Supervisor multi-agent graph; plan → execute → review loop; LLM reserved
> for decisions that need judgment; a rule engine for deterministic extraction;
> memories, observability, and an offline golden-set eval to prove it.
> R: End-to-end run on hnbn666.cn: 6/6 sections saved, 0 failures; 117 tests
> green in CI; second crawl of the same site hit 100% memory reuse.
