"""tests/test_react_loop.py — Function Calling / ReAct 工具调用循环测试

核心方法论：用 FakeLLM 桩掉真实模型，只测"决策解析 → 工具执行 → 结果回填
→ 收敛回答"的循环逻辑（面试叙事：Agent 决策链如何做 mock 测试）。
"""

import asyncio
import json

import pytest

from agents.react import (
    FunctionCallingLoop,
    ToolCall,
    parse_tool_calls,
)
from agents.tools import Tool, ToolRegistry


# ── 工具：翻倍 ──

def _double_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(
        "double", "把整数翻倍",
        {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
        lambda n: n * 2,
    ))
    return reg


class FakeLLM:
    """桩模型：按脚本依次返回预设响应（dict 或 str）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.received_messages = []

    async def ainvoke(self, messages, **kw):
        self.calls += 1
        self.received_messages.append([dict(m) for m in messages])
        return self.script.pop(0)


def _tool_call_resp(name, args, cid="call_1"):
    return {"content": None, "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}},
    ]}


# ── parse_tool_calls ──

def test_parse_structured_tool_calls():
    resp = _tool_call_resp("double", {"n": 21}, cid="c1")
    calls = parse_tool_calls(resp)
    assert len(calls) == 1
    assert calls[0].name == "double"
    assert calls[0].arguments == {"n": 21}
    assert calls[0].id == "c1"


def test_parse_text_marker_tool_calls():
    resp = {"content": "我需要计算：\n```tool\n{\"name\": \"double\", \"arguments\": {\"n\": 5}}\n```"}
    calls = parse_tool_calls(resp)
    assert len(calls) == 1
    assert calls[0].name == "double"
    assert calls[0].arguments == {"n": 5}


def test_parse_plain_text_returns_empty():
    assert parse_tool_calls({"content": "没有工具调用"}) == []


def test_parse_malformed_arguments_degrades():
    resp = {"content": None, "tool_calls": [
        {"id": "x", "function": {"name": "double", "arguments": "{bad json"}},
    ]}
    calls = parse_tool_calls(resp)
    assert calls[0].arguments == {}


# ── 循环闭环 ──

def test_loop_plans_executes_and_converges():
    llm = FakeLLM([
        _tool_call_resp("double", {"n": 21}),
        "计算结果是 42",
    ])
    loop = FunctionCallingLoop(llm, _double_registry(), max_rounds=4)
    result = asyncio.run(loop.run([{"role": "user", "content": "21 翻倍是多少？"}]))
    assert result["answer"] == "计算结果是 42"
    assert result["rounds"] == 2
    assert llm.calls == 2
    # 工具结果已回填：第二轮消息里应包含 tool 角色消息
    assert any(m["role"] == "tool" for m in llm.received_messages[1])
    assert loop.trace == [{"round": 1, "calls": ["double"]}]


def test_loop_integrates_with_builtin_registry():
    llm = FakeLLM([
        _tool_call_resp("url_key", {"url": "http://a.com/p?id=1&utm_source=x"}, cid="c1"),
        "归一化后的 URL 键是 a.com/p?id=1",
    ])
    loop = FunctionCallingLoop(llm, ToolRegistry.builtin())
    result = asyncio.run(loop.run([{"role": "user", "content": "归一化这个 URL"}]))
    assert result["answer"] == "归一化后的 URL 键是 a.com/p?id=1"


def test_loop_handles_unregistered_tool():
    llm = FakeLLM([
        _tool_call_resp("not_exist", {}, cid="c1"),
        "该工具不可用",
    ])
    loop = FunctionCallingLoop(llm, _double_registry())
    result = asyncio.run(loop.run([{"role": "user", "content": "调用不存在的工具"}]))
    assert result["answer"] == "该工具不可用"
    # 回填的消息中应包含未注册提示
    joined = "".join(m.get("content", "") for m in llm.received_messages[1])
    assert "未注册" in joined


def test_loop_max_rounds_cap():
    # 模型每次都要求调工具 → 触发轮次上限（防死循环 / 成本兜底）
    llm = FakeLLM([_tool_call_resp("double", {"n": 1})] * 5)
    loop = FunctionCallingLoop(llm, _double_registry(), max_rounds=3)
    result = asyncio.run(loop.run([{"role": "user", "content": "一直调用"}]))
    assert result.get("exceeded") is True
    assert result["rounds"] == 3
    assert llm.calls == 3


def test_loop_no_tool_returns_direct_answer():
    llm = FakeLLM(["直接回答，不调工具"])
    loop = FunctionCallingLoop(llm, _double_registry())
    result = asyncio.run(loop.run([{"role": "user", "content": "hi"}]))
    assert result["answer"] == "直接回答，不调工具"
    assert result["rounds"] == 1


def test_tool_schemas_exported():
    reg = _double_registry()
    loop = FunctionCallingLoop(FakeLLM([]), reg)
    schemas = loop.tool_schemas()
    assert schemas[0]["type"] == "function"
    assert schemas[0]["function"]["name"] == "double"


def test_tool_call_dataclass_defaults():
    tc = ToolCall(id="a", name="double")
    assert tc.arguments == {}
