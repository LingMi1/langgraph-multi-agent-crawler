"""tests/test_context_compaction.py — 工作记忆：上下文压缩（compact_history）测试

面试叙事：Agent 循环的上下文不能无限膨胀。本组用例验证
  - token 预算触发压缩，窗口外历史折叠为摘要（"被记住"而非"被丢弃"）
  - system 与最近推理帧（模型收敛所需）原样保留
  - LLM 摘要优先、规则摘要兜底（双保险，压缩永不因摘要失败而崩溃）
  - 与 FunctionCallingLoop 集成：压缩事件进入 trace，循环仍正常收敛
"""

import asyncio
import json

from agents.react import (
    FunctionCallingLoop,
    compact_history,
)
from agents.tools import Tool, ToolRegistry


def _tool_frame(name, args, output, content=""):
    """构造一轮 assistant(带 tool_calls) + tool(结果) 的标准消息帧。"""
    return [
        {"role": "assistant", "content": content,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": name,
                                      "arguments": json.dumps(args, ensure_ascii=False)}}]},
        {"role": "tool", "tool_call_id": "c1", "content": output},
    ]


def _big_history(n_frames=2, output_len=3000):
    """构造：system + n_frames 轮工具调用（每轮输出很长，必然超小预算）。"""
    h = [{"role": "system", "content": "sys"}]
    for i in range(n_frames):
        h += _tool_frame("double", {"n": i}, "x" * output_len)
    return h


def _summary_of(history):
    """压缩结果的摘要消息（system 之后的 assistant 消息）。"""
    return history[1]["content"]


# ── 压缩触发条件 ──

def test_no_compact_when_under_budget():
    h = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    out, stats, did = compact_history(h, max_tokens=8000)
    assert did is False
    assert stats["mode"] == "none"
    assert len(out) == len(h)


def test_no_compact_when_messages_too_few():
    # 消息数 <= keep_recent+1 时即使超预算也不压缩（避免高频无意义压缩）
    h = _big_history(n_frames=1, output_len=5000)  # 3 条消息
    out, _, did = compact_history(h, max_tokens=100, keep_recent=2)
    assert did is False


def test_compact_preserves_system_and_recent_keeps_old_collapsed():
    h = _big_history(n_frames=3, output_len=3000)  # 7 条消息
    out, stats, did = compact_history(h, max_tokens=500, keep_recent=2)
    assert did is True
    assert stats["mode"] == "rule"
    assert stats["after"] < stats["before"]
    # system 保留
    assert out[0]["role"] == "system"
    assert out[0]["content"] == "sys"
    # 窗口外历史被折叠为一条 assistant 摘要
    assert out[1]["role"] == "assistant"
    assert "调用 double" in _summary_of(out)
    # 最近 keep_recent 条推理帧原样保留（最后两条 = 最新一轮的 assistant+tool）
    assert out[-2:] == h[-2:]


# ── 摘要策略：LLM 优先、规则兜底 ──

def test_rule_summary_reflects_tool_status():
    h = [{"role": "system", "content": "s"}] + _tool_frame(
        "double", {"n": 1}, "ok") + _tool_frame(
        "double", {"n": 2}, "[工具执行失败] boom") + _tool_frame(
        "double", {"n": 3}, "ok2")
    out, _, did = compact_history(h, max_tokens=1, keep_recent=1)
    assert did is True
    summary = _summary_of(out)
    assert "调用 double" in summary
    assert "成功" in summary      # 成功结果在摘要中可见
    assert "失败" in summary      # 失败结果被如实保留，不因压缩而丢失诊断信息


def test_llm_summarizer_preferred():
    h = _big_history(n_frames=3, output_len=3000)
    calls = []

    def fake_summarizer(text):
        calls.append(text)
        return "【LLM 摘要】已完成全部页面抓取"

    out, stats, did = compact_history(h, max_tokens=500, keep_recent=2,
                                      summarizer_fn=fake_summarizer)
    assert did is True
    assert stats["mode"] == "llm"
    assert len(calls) == 1
    assert "double" in calls[0]          # 摘要输入确实来自窗口外历史
    assert _summary_of(out) == "【LLM 摘要】已完成全部页面抓取"


def test_llm_summarizer_failure_falls_back_to_rule():
    h = _big_history(n_frames=3, output_len=3000)

    def broken_summarizer(text):
        raise RuntimeError("模型挂了")

    out, stats, did = compact_history(h, max_tokens=500, keep_recent=2,
                                      summarizer_fn=broken_summarizer)
    assert did is True
    assert stats["mode"] == "rule"       # 兜底成功，不崩溃
    assert "调用 double" in _summary_of(out)


def test_empty_summarizer_output_falls_back_to_rule():
    h = _big_history(n_frames=3, output_len=3000)
    out, stats, _ = compact_history(h, max_tokens=500, keep_recent=2,
                                    summarizer_fn=lambda text: "   ")
    assert stats["mode"] == "rule"
    assert "调用 double" in _summary_of(out)


# ── 与 FunctionCallingLoop 集成 ──

class FakeLLM:
    """桩模型：按脚本依次返回预设响应，并记录每次收到的消息（供压缩断言）。"""

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


def _big_output_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(
        "big", "返回超长结果（制造上下文压力）",
        {"type": "object", "properties": {}, "required": []},
        lambda: "x" * 5000,
    ))
    return reg


def test_loop_compacts_and_still_converges():
    llm = FakeLLM([
        _tool_call_resp("big", {}),
        _tool_call_resp("big", {}),
        {"content": "完成"},
    ])
    loop = FunctionCallingLoop(
        llm, _big_output_registry(),
        max_rounds=3,
        max_context_tokens=2000,     # 小预算：第二轮 5KB 结果入史后必触发压缩
        keep_recent_messages=2,
    )
    result = asyncio.run(loop.run([{"role": "user", "content": "开始"}]))

    # 循环仍收敛，未因压缩改变行为
    assert result["answer"] == "完成"
    assert result["rounds"] == 3

    # trace 记录了一次压缩事件（可观测性）
    compactions = [t for t in loop.trace if t.get("event") == "context_compact"]
    assert len(compactions) == 1
    assert compactions[0]["mode"] in ("llm", "rule")
    assert compactions[0]["after"] < compactions[0]["before"]

    # 第三轮发给 LLM 的消息里已含摘要（窗口外历史"被记住"）
    last_sent = llm.received_messages[-1]
    assert any("调用 big" in str(m.get("content", "")) for m in last_sent)
    # 且最新的工具结果帧仍原样存在（模型仍能基于最新结果推理）
    assert any(m.get("role") == "tool" for m in last_sent)
