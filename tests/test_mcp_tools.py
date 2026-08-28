"""MCP server 工具层单测（不拉子进程、不真网络；协议级双向链路由 tools/mcp_client.py 真跑覆盖）。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.mcp_server import _TOOLS, _dispatch, _on_call_tool, build_server

_SAMPLE = "郑州天筑膜结构工程有限公司主营膜结构车棚设计与安装，" * 10


def test_tool_schemas_match_react_registry():
    """schema 与 ReAct 接管工具集同一口径（三工具齐、入参 schema 带 required）。"""
    names = {t.name for t in _TOOLS}
    assert names == {"fetch_page", "apply_config", "quality_judge"}
    fp = next(t for t in _TOOLS if t.name == "fetch_page")
    assert fp.input_schema.get("required") == ["url"]


def test_dispatch_quality_judge():
    r = _dispatch("quality_judge", {"sample": _SAMPLE})
    assert 0.0 <= float(r.get("score", 0)) <= 1.0


def test_dispatch_apply_config_whitelist():
    r = _dispatch("apply_config", {"needs_js_render": True, "request_delay": 99})
    assert r == {"needs_js_render": True, "request_delay": 10.0}  # 延迟钳到上限


def test_dispatch_unknown_tool_fail_closed():
    try:
        _dispatch("no_such_tool", {})
        raised = False
    except KeyError:
        raised = True
    assert raised


def test_on_call_tool_error_channel():
    """工具异常 → CallToolResult.isError=True + JSON 错误体（不崩 server）。"""
    class _Params:  # 轻量参数桩（避免依赖 SDK 内部请求类型细节）
        name = "no_such_tool"
        arguments = {}

    result = asyncio.run(_on_call_tool(None, _Params()))
    assert result.is_error is True
    assert "no_such_tool" in json.loads(result.content[0].text)["error"]


def test_on_call_tool_success_channel():
    class _Params:
        name = "quality_judge"
        arguments = {"sample": _SAMPLE}

    result = asyncio.run(_on_call_tool(None, _Params()))
    assert result.is_error is False
    assert "score" in json.loads(result.content[0].text)


def test_build_server_registers_handlers():
    server = build_server()
    assert server.server_info.name == "crawler-tools"
    # MCP 2.x：tools/list 与 tools/call 请求处理器已注册
    assert server.get_request_handler("tools/list") is not None
    assert server.get_request_handler("tools/call") is not None
