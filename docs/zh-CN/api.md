# API 参考

FastAPI 服务在 `api/server.py`。所有端点在一个进程里；服务层**零业务逻辑**——CLI、桌面 GUI、REST 共用同一个 `run_langgraph_crawler` 核心。

**鉴权**：设置 `CRAWLER_API_KEY` 后，每个端点都要带 `X-API-Key` 头（或 `Authorization: Bearer <key>`），用 `secrets.compare_digest` 恒定时间比较。

## 爬取任务

### `POST /crawl`
提交一个站点开始采集。

- 请求体（JSON）：`{"url": "...", "concurrency": 5}`
- 响应：`202 Accepted` 返回 `task_id`；单爬虫槽被占返回 `409`；客户端被限流返回 `429`（60s 滑动窗口 6 次提交）。

### `GET /tasks/{task_id}`
任务状态与进度（含 SSE 事件日志）。

### `GET /tasks/{task_id}/results`
任务已采集的页面。

### `POST /orchestrator/stream`
Web 工作台用的 SSE 事件流（`page_crawled`、`node_start`、`node_end`、`retry`、`done` 等）。一次采集会话一个请求；进度字段含 `page_count`、`pending`、`total`，供动态进度条使用。

## LLM 配置

| 端点 | 用途 |
|---|---|
| `GET /llm/providers` | 列出已配置的 provider |
| `POST /llm/providers` | 新增/更新 provider |
| `DELETE /llm/providers/{pid}` | 删除 provider |
| `POST /llm/config` | 应用 provider 路由配置（web / 桌面 GUI / CLI 三处同步） |
| `POST /llm/keys` | 保存 API key（服务端） |
| `POST /llm/keys/test` | 对 provider 做连通性测试 |

路由配置是唯一真相源：FastAPI 服务、桌面 GUI、CLI 都读同一份已应用的配置。

## 结果与历史

### `GET /orchestrator/result/{result_dir}/{filename}`
单页原始内容（按 CSV 行的 `_file_id` 反查，不读磁盘）。重复内容行返回明确的"重复内容、未单独存储"提示页。

### `GET /orchestrator/cleaned/{result_dir}/{filename}`
页面的清洗后（规则处理后）版本，当该次运行存在清洗输出目录时可用。

### `POST /orchestrator/save/{result_dir}/{filename}`
把编辑后的页面写回运行目录。请求体：`{"content": "..."}`。

### `GET /orchestrator/classification?result_dir=…&leaf_only=…`
页面级元数据：`ywlx1-4` 栏目层级、真实标题、重复标记——用来构建工作台左侧的栏目树。

### `GET /orchestrator/export_csv`
导出本次运行的 `content_001` CSV（带栏目列，供导入数据库）。

### `GET /orchestrator/output_zip?result_dir=…&leaf_only=true|false`
服务端打包运行输出目录：`<域名>/<栏目>/<标题>.html` + `crawl_results.csv`。重复占位行排除。工作台"保存全部结果"按钮靠它：浏览器选目录后解包写入；不支持选目录的浏览器直接下载 zip。

### `POST /orchestrator/import_db`
把一次运行导入数据库（工作台一键导入）。

## 历史记录（服务端 SQLite）

| 端点 | 用途 |
|---|---|
| `GET /history` | 列出采集历史记录（运行 + 站点页面） |
| `POST /history` | 保存一次完成的运行快照 |
| `GET /history/{hid}` | 单条记录（页面 + 指标） |
| `DELETE /history/{hid}` | 删除单条记录 |
| `DELETE /history` | 清空全部历史 |

## 流式对话

### `POST /chat/stream`
LLM 管线 `chat_stream` 路径的 SSE 流式端点（运行级熔断下的多 provider 故障转移）。

## 错误约定

- `404` — 结果目录 / 文件 / 历史记录不存在
- `409` — 爬取槽被占
- `429` — 客户端被限流
- `401` — API key 缺失或不匹配（启用鉴权时）
