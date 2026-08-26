"""agents/react.py — 轻量 Function Calling / ReAct 工具调用循环

面试叙事：ToolRegistry 只提供"能力声明"（schema），真正的 Agent 化是
LLM 在对话中**决策调用工具 → 执行结果回填 → 继续推理 → 收敛回答**。
本模块用一段无框架依赖的循环把这条链路打通：

  用户 → LLM → 解析 tool_calls → ToolRegistry.call → 结果回填 → LLM → 收敛

支持两种工具调用格式：
  - 结构化 tool_calls（OpenAI 兼容，response.tool_calls）
  - 文本式标记（内容中的 ```tool\n{"name":..., "arguments":{...}}```，便于 mock/降级）

设计取舍：
  - 不引入 langchain.agents，自研循环便于讲解与测试（面试：能讲清每一步）
  - 工具执行与 LLM 解耦：llm 只要求兼容 ainvoke(messages)，工具走 ToolRegistry
  - 每轮工具结果都以 "tool" 角色消息回填，模型可基于结果继续推理
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agents.tools import ToolRegistry

# 文本式工具调用标记：```tool\n{"name": ..., "arguments": {...}}\n```
_TEXT_TOOL_RE = re.compile(r"```tool\s*(\{.*?\})\s*```", re.S)


@dataclass
class ToolCall:
    """一次工具调用意图：id / 工具名 / 参数（已解析为 dict）。"""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


def _raw_tool_calls(response: Any) -> Optional[list]:
    """从响应中取原始 tool_calls 列表（兼容 dict 与对象）。"""
    if isinstance(response, dict):
        return response.get("tool_calls")
    return getattr(response, "tool_calls", None)


def _content_of(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        return response.get("content") or ""
    return getattr(response, "content", None) or ""


def _parse_text_marker(text: str) -> List[ToolCall]:
    """解析 ```tool\n{...}``` 文本标记（降级 / mock 用）。"""
    calls = []
    for i, m in enumerate(_TEXT_TOOL_RE.finditer(text or "")):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        calls.append(ToolCall(
            id=obj.get("id") or f"txt_{i}",
            name=obj.get("name", ""),
            arguments=obj.get("arguments") or {},
        ))
    return calls


def parse_tool_calls(response: Any) -> List[ToolCall]:
    """从 LLM 响应中提取工具调用列表（结构化优先，文本标记兜底）。"""
    if isinstance(response, str):
        return _parse_text_marker(response)
    raw = _raw_tool_calls(response)
    if raw:
        calls = []
        for i, tc in enumerate(raw):
            if isinstance(tc, dict):
                name = tc.get("function", {}).get("name", "")
                args_raw = tc.get("function", {}).get("arguments", "{}")
            else:
                name = tc.function.name
                args_raw = tc.function.arguments
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(
                id=tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None) or f"call_{i}",
                name=name,
                arguments=args,
            ))
        return calls
    text = _content_of(response)
    if text:
        return _parse_text_marker(text)
    return []


def _assistant_msg(response: Any, calls: List[ToolCall]) -> Dict[str, Any]:
    """构造带 tool_calls 的 assistant 消息（OpenAI 兼容格式）。"""
    return {
        "role": "assistant",
        "content": _content_of(response) or "",
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
            }
            for c in calls
        ],
    }


class FunctionCallingLoop:
    """ReAct 工具调用循环：LLM 决策 → 执行工具 → 回填 → 收敛。

    Args:
        llm: 兼容 ainvoke(messages) 的客户端（可包 TrackedLLM 自动记账/重试）
        tools: ToolRegistry（工具能力声明 + 执行）
        max_rounds: 最大工具调用轮数（防死循环，成本兜底）
    """

    def __init__(self, llm: Any, tools: ToolRegistry, max_rounds: int = 4):
        self._llm = llm
        self._tools = tools
        self.max_rounds = max_rounds
        self.trace: List[Dict[str, Any]] = []   # 每轮工具决策记录（可观测）

    def tool_schemas(self) -> list:
        return self._tools.all_schemas()

    async def run(self, messages: List[Dict[str, Any]],
                  system: Optional[str] = None) -> Dict[str, Any]:
        """执行循环。messages 为 [{role, content}...]；返回 {answer, rounds, trace}。"""
        history = ([{"role": "system", "content": system}] if system else []) + [dict(m) for m in messages]
        self.trace = []

        for rnd in range(1, self.max_rounds + 1):
            resp = await self._llm.ainvoke(history)
            calls = parse_tool_calls(resp)
            if not calls:
                return {"answer": _content_of(resp) or None, "rounds": rnd, "trace": self.trace}

            history.append(_assistant_msg(resp, calls))
            results = []
            for c in calls:
                try:
                    out = self._tools.call(c.name, **c.arguments)
                    results.append({"id": c.id, "ok": True, "output": out})
                except KeyError as e:
                    results.append({"id": c.id, "ok": False, "output": f"[工具未注册] {e}"})
            self.trace.append({"round": rnd, "calls": [c.name for c in calls]})
            for r in results:
                history.append({"role": "tool", "tool_call_id": r["id"],
                                "content": str(r["output"])})

        return {"answer": None, "rounds": self.max_rounds, "trace": self.trace,
                "exceeded": True}
