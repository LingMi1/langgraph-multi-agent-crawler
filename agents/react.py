"""agents/react.py — 轻量 Function Calling / ReAct 工具调用循环

面试叙事：ToolRegistry 只提供"能力声明"（schema），真正的 Agent 化是
LLM 在对话中**决策调用工具 → 执行结果回填 → 继续推理 → 收敛回答**。
本模块用一段无框架依赖的循环把这条链路打通：

  用户 → LLM → 解析 tool_calls → ToolRegistry.call → 结果回填 → LLM → 收敛

支持两种工具调用格式：
  - 结构化 tool_calls（OpenAI 兼容，response.tool_calls）
  - 文本式标记（内容中的 ```tool\n{"name":..., "arguments":{...}}```，便于 mock/降级）

上下文管理（工作记忆）：
  - 记忆分层：长期记忆放外部（memory.py 的 SQLite / vector_retriever 的 RAG），
    循环内的对话历史是"工作记忆"，只增不减会随工具轮数线性膨胀。
  - `compact_history` 提供**预算触发的上下文压缩**：token 估算超预算时，
    保留 system + 最近 `keep_recent` 条推理帧（模型收敛仍需要它们），
    窗口外的历史折叠成一条摘要消息（LLM 摘要优先、规则摘要兜底）。
  - 压缩不改变循环语义：最新的 tool 结果始终原样回填，旧帧只是"被记住"而非"被丢弃"。

设计取舍：
  - 不引入 langchain.agents，自研循环便于讲解与测试（面试：能讲清每一步）
  - 工具执行与 LLM 解耦：llm 只要求兼容 ainvoke(messages)，工具走 ToolRegistry
  - 每轮工具结果都以 "tool" 角色消息回填，模型可基于结果继续推理
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from agents.budget import estimate_tokens
from agents.tools import ToolRegistry, sanitize_tool_args

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


def _preview(obj: Any, limit: int = 120) -> str:
    """把工具入参/出参压缩为可审计的短摘要（日志友好，防超长内容撑爆审计）。"""
    try:
        s = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        s = str(obj)
    return s[:limit]


# ── 上下文压缩（工作记忆）：token 预算触发的滑动窗口 + 摘要 ──


def _msg_tokens(m: Dict[str, Any]) -> int:
    """单条消息的启发式 token 估算（复用 budget.estimate_tokens，成本口径一致）。"""
    content = m.get("content")
    if isinstance(content, str):
        return estimate_tokens(content)
    return 0


def _old_messages_text(old: List[Dict[str, Any]]) -> str:
    """把窗口外消息渲染成可喂给摘要模型的文本（内容裁剪，防二次膨胀）。"""
    lines = []
    for m in old:
        role = m.get("role")
        if role == "assistant":
            tcs = m.get("tool_calls") or []
            for tc in tcs:
                fn = (tc.get("function") or {}).get("name", "?")
                lines.append(f"[assistant] 调用工具 {fn}: {_preview(m.get('content'), 60)}")
            if not tcs:
                lines.append(f"[assistant] {_preview(m.get('content'), 120)}")
        elif role == "tool":
            lines.append(f"[tool] {_preview(m.get('content'), 120)}")
        else:
            lines.append(f"[{role}] {_preview(m.get('content'), 120)}")
    return "\n".join(lines)


def _rule_summary(old: List[Dict[str, Any]]) -> str:
    """规则式摘要（LLM 摘要不可用时的兜底，永远不失败）：
    只保留"调用了哪个工具 → 结果成败/前几个字"，丢掉过程细节。"""
    parts = []
    for m in old:
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {}).get("name", "?")
                parts.append(f"调用 {fn}")
        elif role == "tool":
            content = str(m.get("content", ""))
            ok = "失败" if content.startswith("[工具执行失败]") else "成功"
            parts.append(f"→ {ok}: {_preview(content, 60)}")
        elif role == "user":
            parts.append(f"用户: {_preview(m.get('content'), 80)}")
    return "；".join(parts)


def compact_history(
    history: List[Dict[str, Any]],
    max_tokens: int = 8000,
    keep_recent: int = 6,
    summarizer_fn: Optional[Callable[[str], str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], bool]:
    """上下文压缩（工作记忆核心）：
    估算 history 的 token，超过 `max_tokens` 且消息数足够多时，
    把窗口外的旧消息折叠为一条摘要消息，保留 system + 最近 `keep_recent` 条。

    - 摘要优先用 `summarizer_fn(old_text) -> str`（可接 LLM）；异常/为空时
      回退到 `_rule_summary`（确定性、零失败）。这是"LLM 增强 + 规则保底"双保险。
    - 纯对话历史（无 tool 帧）同样可压缩，不依赖工具语义。

    Returns: (new_history, stats, did_compact)
      stats = {"mode": "llm"|"rule"|"none", "tokens": before, "after_tokens": after,
               "before": msgs, "after": msgs}
    """
    tokens = sum(_msg_tokens(m) for m in history)
    if tokens <= max_tokens or len(history) <= keep_recent + 1:
        return list(history), {"mode": "none", "tokens": tokens, "after_tokens": tokens,
                               "before": len(history), "after": len(history)}, False

    system = [m for m in history if m.get("role") == "system"]
    others = [m for m in history if m.get("role") != "system"]
    if len(others) <= keep_recent:
        return list(history), {"mode": "none", "tokens": tokens, "after_tokens": tokens,
                               "before": len(history), "after": len(history)}, False
    old, recent = others[:-keep_recent], others[-keep_recent:]

    summary: Optional[str] = None
    mode = "rule"
    if summarizer_fn is not None:
        try:
            raw = summarizer_fn(_old_messages_text(old))
            if raw and str(raw).strip():
                summary = str(raw).strip()
                mode = "llm"
        except Exception:
            summary = None
    if summary is None:
        summary = _rule_summary(old)

    compacted = system + [{"role": "assistant", "content": summary}] + recent
    compacted_tokens = sum(_msg_tokens(m) for m in compacted)
    return compacted, {"mode": mode, "tokens": tokens, "after_tokens": compacted_tokens,
                       "before": len(history), "after": len(compacted)}, True


class FunctionCallingLoop:
    """ReAct 工具调用循环：LLM 决策 → 执行工具 → 回填 → 收敛。

    Args:
        llm: 兼容 ainvoke(messages) 的客户端（可包 TrackedLLM 自动记账/重试）
        tools: ToolRegistry（工具能力声明 + 执行）
        max_rounds: 最大工具调用轮数（防死循环，成本兜底）
        max_context_tokens: 工作记忆 token 预算，超预算触发上下文压缩
        keep_recent_messages: 压缩时保留的最近消息条数（模型收敛所需的推理帧）
        summarizer_fn: 可选摘要函数 old_text -> str（接 LLM）；None 或失败则规则兜底
    """

    def __init__(self, llm: Any, tools: ToolRegistry, max_rounds: int = 4,
                 max_context_tokens: int = 8000, keep_recent_messages: int = 6,
                 summarizer_fn: Optional[Callable[[str], str]] = None):
        self._llm = llm
        self._tools = tools
        self.max_rounds = max_rounds
        self.max_context_tokens = max_context_tokens
        self.keep_recent_messages = keep_recent_messages
        self.summarizer_fn = summarizer_fn
        self.trace: List[Dict[str, Any]] = []   # 每轮工具决策记录（可观测）

    def tool_schemas(self) -> list:
        return self._tools.all_schemas()

    async def run(self, messages: List[Dict[str, Any]],
                  system: Optional[str] = None) -> Dict[str, Any]:
        """执行循环。messages 为 [{role, content}...]；返回 {answer, rounds, trace}。"""
        history = ([{"role": "system", "content": system}] if system else []) + [dict(m) for m in messages]
        self.trace = []

        for rnd in range(1, self.max_rounds + 1):
            # 工作记忆：超预算时压缩窗口外历史，保证 ainvoke 输入不无限膨胀
            history, cstats, did_compact = compact_history(
                history,
                max_tokens=self.max_context_tokens,
                keep_recent=self.keep_recent_messages,
                summarizer_fn=self.summarizer_fn,
            )
            if did_compact:
                self.trace.append({"event": "context_compact", "round": rnd, **cstats})
            resp = await self._llm.ainvoke(history)
            calls = parse_tool_calls(resp)
            if not calls:
                return {"answer": _content_of(resp) or None, "rounds": rnd, "trace": self.trace}

            history.append(_assistant_msg(resp, calls))
            results = []
            for c in calls:
                tool = self._tools.get(c.name)
                if tool is None:
                    # 未知/恶意工具名 → 显式拒绝 + 审计（不打断循环，供复核）
                    self.trace.append({
                        "round": rnd, "tool": c.name, "status": "unknown_tool",
                        "error": f"未注册的工具 {c.name!r}",
                    })
                    results.append({"id": c.id, "ok": False,
                                    "output": f"[工具未注册] {c.name}"})
                    continue
                try:
                    # Tool-Use 安全第一层：按 JSON Schema 净化参数（剥离未知 key /
                    # 截断超长字符串 / 类型强制），再执行
                    cleaned = sanitize_tool_args(c.name, c.arguments, tool.parameters)
                    out = tool(**cleaned)
                    self.trace.append({
                        "round": rnd, "tool": c.name,
                        "args_preview": _preview(c.arguments),       # 模型原始请求（审计证据）
                        "sanitized_preview": _preview(cleaned),      # 净化后实际执行（可复核）
                        "output_preview": _preview(out), "ok": True,
                    })
                    results.append({"id": c.id, "ok": True, "output": out})
                except Exception as e:  # noqa: BLE001 — 工具执行失败必须回填可诊断信息
                    self.trace.append({
                        "round": rnd, "tool": c.name,
                        "args_preview": _preview(c.arguments),
                        "error": str(e), "ok": False,
                    })
                    results.append({"id": c.id, "ok": False,
                                    "output": f"[工具执行失败] {e}"})
            for r in results:
                history.append({"role": "tool", "tool_call_id": r["id"],
                                "content": str(r["output"])})

        return {"answer": None, "rounds": self.max_rounds, "trace": self.trace,
                "exceeded": True}
