"""tests/test_tool_safety.py — Tool-Use 安全层测试（FC 工具调用三件套）

覆盖：
  1. sanitize_tool_args 参数净化：未知 key 剥离 / 字符串截断 / 类型强制
  2. ToolRegistry.call 内置净化：确定性调用幂等，恶意参数被净化后仍安全执行
  3. FC 循环审计轨迹：入参/出参摘要落 trace；工具失败 / 未知工具显式记录

方法论：工具执行是确定性行为（可复核），LLM 决策用 FakeLLM 桩掉——
"模型的输出不可信，安全边界在工具执行层"（面试叙事）。
"""

import asyncio
import json

from agents.react import FunctionCallingLoop
from agents.tools import Tool, ToolRegistry, sanitize_tool_args


# ── 1. sanitize_tool_args 参数净化 ──

def _schema():
    return {
        "type": "object",
        "properties": {
            "sample": {"type": "string"},
            "max_len": {"type": "integer"},
            "ratio": {"type": "number"},
            "strict": {"type": "boolean"},
            "texts": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["sample"],
    }


def test_unknown_keys_stripped():
    args = {"sample": "ok", "evil": "drop_me", "__import__": "x"}
    out = sanitize_tool_args("t", args, _schema())
    assert out == {"sample": "ok"}


def test_long_string_truncated():
    args = {"sample": "长" * 10000}
    out = sanitize_tool_args("t", args, _schema())
    assert len(out["sample"]) == 4000  # 默认上限


def test_non_string_coerced_to_string():
    out = sanitize_tool_args("t", {"sample": 123}, _schema())
    assert out["sample"] == "123"


def test_invalid_integer_dropped():
    out = sanitize_tool_args("t", {"sample": "x", "max_len": "not_a_num"}, _schema())
    assert "max_len" not in out


def test_integer_string_coerced():
    out = sanitize_tool_args("t", {"sample": "x", "max_len": "50"}, _schema())
    assert out["max_len"] == 50


def test_number_and_boolean_coerced():
    out = sanitize_tool_args(
        "t", {"sample": "x", "ratio": "0.6", "strict": "yes"}, _schema()
    )
    assert out["ratio"] == 0.6
    assert out["strict"] is True


def test_string_array_truncated_per_element():
    out = sanitize_tool_args(
        "t", {"sample": "x", "texts": ["短", "长" * 9999, 7]}, _schema()
    )
    assert len(out["texts"]) == 3
    assert len(out["texts"][1]) == 4000
    assert out["texts"][2] == "7"  # 元素强转字符串


def test_schema_missing_properties_is_noop():
    out = sanitize_tool_args("t", {"sample": "x"}, {})
    assert out == {}


# ── 2. ToolRegistry.call 内置净化（确定性调用幂等） ──

def test_registry_call_normal_args_unchanged():
    reg = ToolRegistry()
    reg.register(Tool(
        "double", "翻倍", {"type": "object", "properties": {"n": {"type": "integer"}},
        "required": ["n"]}, lambda n: n * 2,
    ))
    assert reg.call("double", n=21) == 42
    # 净化幂等：字符串形式的整数也能被强制
    assert reg.call("double", n="21") == 42


def test_registry_call_strips_malicious_args():
    reg = ToolRegistry()
    reg.register(Tool(
        "echo", "回显", {"type": "object", "properties": {"s": {"type": "string"}},
        "required": ["s"]}, lambda s: s,
    ))
    # 未知参数剥离 → 工具只收到 schema 声明的字段
    out = reg.call("echo", s="hello", hidden="malicious")
    assert out == "hello"


def test_registry_builtin_url_key_with_huge_url():
    reg = ToolRegistry.builtin()
    # 超长 URL（含注入尝试）被截断后仍可归一化，不抛异常
    url = "http://a.com/p?id=1&utm_source=x" + "&junk=" + "A" * 20000
    assert isinstance(reg.call("url_key", url=url), str)


# ── 3. FC 循环审计轨迹 ──

class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def ainvoke(self, messages, **kw):
        self.calls += 1
        return self.script.pop(0)


def _tool_call_resp(name, args, cid="call_1"):
    return {"content": None, "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}},
    ]}


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(
        "double", "翻倍", {"type": "object", "properties": {"n": {"type": "integer"}},
        "required": ["n"]}, lambda n: n * 2,
    ))
    return reg


def test_fc_trace_records_args_and_output():
    llm = FakeLLM([
        _tool_call_resp("double", {"n": 21}),
        "结果是 42",
    ])
    loop = FunctionCallingLoop(llm, _registry())
    result = asyncio.run(loop.run([{"role": "user", "content": "21 翻倍"}]))
    assert result["answer"] == "结果是 42"
    assert loop.trace[0]["tool"] == "double"
    assert loop.trace[0]["args_preview"] == '{"n": 21}'
    assert loop.trace[0]["output_preview"] == "42"
    assert loop.trace[0]["ok"] is True


def test_fc_trace_records_failed_tool_execution():
    reg = ToolRegistry()
    reg.register(Tool(
        "boom", "必定失败", {"type": "object", "properties": {"x": {"type": "string"}},
        "required": ["x"]}, lambda x: (_ for _ in ()).throw(RuntimeError("执行器异常")),
    ))
    llm = FakeLLM([
        _tool_call_resp("boom", {"x": "y"}),
        "工具失败了",
    ])
    loop = FunctionCallingLoop(llm, reg)
    result = asyncio.run(loop.run([{"role": "user", "content": "调用 boom"}]))
    assert result["answer"] == "工具失败了"  # 失败不打断循环
    entry = loop.trace[0]
    assert entry["ok"] is False and entry["tool"] == "boom"
    assert "执行器异常" in entry["error"]


def test_fc_unknown_tool_rejected_and_audited():
    llm = FakeLLM([
        _tool_call_resp("not_exist", {}),
        "无法调用",
    ])
    loop = FunctionCallingLoop(llm, _registry())
    result = asyncio.run(loop.run([{"role": "user", "content": "调用未知工具"}]))
    assert result["answer"] == "无法调用"
    assert loop.trace[0]["status"] == "unknown_tool"
    assert loop.trace[0]["tool"] == "not_exist"


def test_fc_malicious_args_are_sanitized_before_execution():
    # LLM 传入 schema 外的注入参数 → 被剥离，工具仍按声明字段执行
    llm = FakeLLM([
        _tool_call_resp("double", {"n": 21, "evil": "注入参数"}),
        "结果是 42",
    ])
    loop = FunctionCallingLoop(llm, _registry())
    result = asyncio.run(loop.run([{"role": "user", "content": "翻倍"}]))
    assert result["answer"] == "结果是 42"
    # 审计同时保留"模型说了什么"（原始请求）与"实际执行了什么"（净化后）
    assert loop.trace[0]["args_preview"] == '{"evil": "注入参数", "n": 21}'
    assert loop.trace[0]["sanitized_preview"] == '{"n": 21}'  # evil 已被剥离
