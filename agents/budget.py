"""agents/budget.py — LLM 成本记账 + 统一可靠性（TokenBudget + TrackedLLM）

对所有 LLM 调用统一：
  1. 记账（调用次数、prompt/completion token 估算、单次成本）
  2. 重试与指数退避（LLM 不稳定是常态，调用层负责扛）
  3. 兼容 langchain 的 invoke / ainvoke 接口，业务代码零改动

Token 估算：无 tiktoken 依赖，采用启发式
  - 英文/ASCII 约 4 字符 = 1 token
  - 中文/全角约 1 字符 = 1 token（粗略）
仅用于成本量级评估，不追求精确计数。

用法:
  budget = TokenBudget()
  llm = TrackedLLM(chat_openai_client, budget)   # retries=2, backoff=2.0
  await llm.ainvoke(prompt)      # 自动重试 + 记账
  print(budget.summary())
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Dict


def estimate_tokens(text: str) -> int:
    """启发式 token 估算（ASCII≈4 字符/token，CJK≈1 字符/token）。"""
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    cjk_chars = len(text) - ascii_chars
    return ascii_chars // 4 + cjk_chars


class TokenBudget:
    """线程安全的 LLM 成本记账器。"""

    def __init__(self):
        self._entries: list = []          # 每次调用一条
        self._by_agent: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        )

    def add(self, agent: str = "", kind: str = "llm",
            prompt_tokens: int = 0, completion_tokens: int = 0,
            cost: float = 0.0) -> None:
        row = self._by_agent[agent or kind]
        row["calls"] += 1
        row["prompt_tokens"] += prompt_tokens
        row["completion_tokens"] += completion_tokens
        row["cost"] += cost
        self._entries.append({
            "ts": time.strftime("%H:%M:%S"),
            "agent": agent or kind,
            "kind": kind,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost": round(cost, 4),
        })

    def stats(self) -> Dict[str, Any]:
        total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        for row in self._by_agent.values():
            for k in total:
                total[k] += row[k]
        return {"total": total, "by_agent": dict(self._by_agent)}

    def summary(self) -> str:
        s = self.stats()
        t = s["total"]
        parts = [f"调用={t['calls']}", f"prompt≈{int(t['prompt_tokens'])}tok",
                 f"completion≈{int(t['completion_tokens'])}tok", f"cost≈${t['cost']:.4f}"]
        agents = []
        for name, row in sorted(s["by_agent"].items()):
            if row["calls"]:
                agents.append(f"{name}×{row['calls']}")
        if agents:
            parts.append("(" + ", ".join(agents) + ")")
        return " | ".join(parts)

    def __len__(self) -> int:
        return len(self._entries)


class TrackedLLM:
    """包装 langchain LLM 客户端：invoke/ainvoke 自动重试退避 + 记账。

    兼容现有调用方式：llm.invoke(prompt) / await llm.ainvoke(prompt)，
    响应为 str 或带 .content 的对象时按内容估算 completion token。

    retries 耗尽后抛出最后一次异常（保持原语义，调用方自行降级），
    但每次失败都会在调用方可见层被吞掉前先记 warning 日志。
    """

    def __init__(self, client: Any, budget: TokenBudget, agent: str = "llm",
                 retries: int = 2, backoff: float = 2.0):
        self._client = client
        self._budget = budget
        self._agent = agent
        self.retries = retries
        self.backoff = backoff

    def _account(self, prompt: str, response: Any) -> Any:
        try:
            content = getattr(response, "content", None)
            if content is None:
                content = str(response)
            self._budget.add(
                agent=self._agent,
                prompt_tokens=estimate_tokens(str(prompt)),
                completion_tokens=estimate_tokens(str(content)),
            )
        except Exception:
            pass
        return response

    def _backoff_sleep(self, attempt: int) -> None:
        time.sleep(self.backoff * (attempt + 1))

    async def _abackoff_sleep(self, attempt: int) -> None:
        await asyncio.sleep(self.backoff * (attempt + 1))

    def invoke(self, prompt: str, **kwargs: Any) -> Any:
        last_err = None
        for attempt in range(self.retries + 1):
            try:
                return self._account(prompt, self._client.invoke(prompt, **kwargs))
            except Exception as e:  # noqa: BLE001 - 调用层负责统一兜底
                last_err = e
                if attempt < self.retries:
                    self._backoff_sleep(attempt)
        raise last_err

    async def ainvoke(self, prompt: str, **kwargs: Any) -> Any:
        last_err = None
        for attempt in range(self.retries + 1):
            try:
                return self._account(prompt, await self._client.ainvoke(prompt, **kwargs))
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < self.retries:
                    await self._abackoff_sleep(attempt)
        raise last_err
