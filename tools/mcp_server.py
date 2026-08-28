"""
MCP Server（Model Context Protocol，stdio 传输）— 把爬虫的行动/分析工具暴露为标准 MCP 工具。

面试叙事定位：
  - MCP 层是"薄适配"：工具逻辑单源复用（fetch_page/apply_config 来自 ReAct 接管、
    quality_judge 来自 FC 评估链路），零重复实现，任何 MCP 客户端
    （Claude Desktop / Cursor / 自研 Agent）即插即用；
  - 与项目内 FunctionCallingLoop 的区别：FC 是"进程内 LLM→工具"私有协议，
    MCP 是跨进程/跨厂商的标准协议（JSON-RPC over stdio）。

运行：
  python tools/mcp_server.py          # stdio 模式（供 MCP 客户端拉起）
  python tools/mcp_client.py          # 配套演示客户端（拉起本 server 并调通三工具）

依赖：mcp>=2.0.0（SDK 2.x handler 构造式 API，FastMCP 已拆分为独立包）。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

# ── 工具执行器：单源复用项目真实能力（与 ReAct 接管 / FC 评估共用同一实现） ──
from graph.react_takeover import _exec_fetch_page, _exec_apply_config
from agents.eval import heuristic_score

# 工具 schema 与 graph/react_takeover.react_tools() 保持同一口径
_TOOLS = [
    types.Tool(
        name="fetch_page",
        description=(
            "对指定 URL 做一次侦察式抓取（HTTP 直连，15s 超时），返回状态码/最终URL/"
            "内容长度/标题预览；用于确认站点当前是否可达、页面是否空壳或被反爬拦截。"
        ),
        inputSchema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    ),
    types.Tool(
        name="apply_config",
        description=(
            "生成新的抓取配置片段（白名单字段）：needs_js_render=启用JS渲染；"
            "user_agent=自定义UA；request_delay=请求间延迟秒数；use_system_chrome=使用系统Chrome。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "needs_js_render": {"type": "boolean"},
                "user_agent": {"type": "string"},
                "request_delay": {"type": "number"},
                "use_system_chrome": {"type": "boolean"},
            },
        },
    ),
    types.Tool(
        name="quality_judge",
        description="对样本文本做确定性质量打分（正文长度/链接密度/图片，0~1 分+理由）。",
        inputSchema={
            "type": "object",
            "properties": {
                "sample": {"type": "string"},
                "criteria": {"type": "string", "default": ""},
            },
            "required": ["sample"],
        },
    ),
]


def _dispatch(name: str, arguments: dict) -> dict:
    """工具调度：未知工具 fail-closed（与 ToolRegistry.call 同语义）。"""
    if name == "fetch_page":
        return _exec_fetch_page(url=str(arguments.get("url", "")))
    if name == "apply_config":
        return _exec_apply_config(
            needs_js_render=arguments.get("needs_js_render"),
            user_agent=str(arguments.get("user_agent") or ""),
            request_delay=arguments.get("request_delay"),
            use_system_chrome=arguments.get("use_system_chrome"),
        )
    if name == "quality_judge":
        return heuristic_score(
            str(arguments.get("sample", "")), str(arguments.get("criteria") or "")
        )
    raise KeyError(f"未注册的 MCP 工具: {name} (可用: {[t.name for t in _TOOLS]})")


async def _on_list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=_TOOLS)


async def _on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        result = _dispatch(params.name, dict(params.arguments or {}))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=True))],
            isError=False,
        )
    except Exception as e:  # noqa: BLE001 — 工具异常回填可诊断信息，不崩 server
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(
                {"error": f"{type(e).__name__}: {e}"}, ensure_ascii=True))],
            isError=True,
        )


def build_server() -> Server:
    return Server(
        "crawler-tools",
        version="1.0.0",
        title="Crawler Agent Tools",
        description="多 Agent 爬虫的行动/分析工具集（fetch_page / apply_config / quality_judge）",
        instructions="侦察抓取→fetch_page；生成重抓配置→apply_config；样本质量打分→quality_judge。",
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )


async def main() -> None:
    server = build_server()
    options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options)


if __name__ == "__main__":
    asyncio.run(main())
