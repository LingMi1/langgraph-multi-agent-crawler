# -*- coding: utf-8 -*-
"""agents/breaker.py — LLM 运行级熔断器（深降级可靠性第一层）

设计（grill-me 定稿）：
  - 连续 N 次（默认 3）LLM 调用失败（重试耗尽口径）→ 熔断打开
  - 单次成功即清零连续计数（端点半死不活时抖动重计）
  - 熔断打开后本 run 内不复位（无 half-open）：端点恢复靠下次进程重启，
    避免半死端点反复拖慢确定性主链路（zztzmjg 实测：每页 30s+ 超时重试）
  - 熔断打开时所有 LLM 入口快速失败：
      chat_json → 返回 None（调用方按"无 LLM"降级）
      TrackedLLM → 抛 CircuitOpenError（调用方 except 降级启发式）
  - 语义：熔断只禁 LLM，不禁爬取——确定性链路全速继续

每次 run_crawler / reset_llm 时 reset()，run 级隔离。
"""

from __future__ import annotations

import time
from typing import Any

import config
from schemas import agent_logger


class CircuitOpenError(RuntimeError):
    """熔断打开时 LLM 调用被快速拒绝（调用方降级，不再等超时）。"""


class LLMCircuitBreaker:
    """运行级熔断器：连续失败计数 + 一次性开闸告警。"""

    def __init__(self, threshold: int = 3):
        self.threshold = max(1, int(threshold))
        self.reset()

    # ── 状态 ──
    @property
    def open(self) -> bool:
        return self._open

    @property
    def reason(self) -> str:
        return self._reason

    def check(self) -> bool:
        """调用前检查：False = 熔断打开，调用方直接降级（不发起请求）。"""
        return not self._open

    # ── 记录 ──
    def record_success(self) -> None:
        """单次成功清零连续失败计数（run 内已开闸则忽略，不复位）。"""
        if self._open:
            return
        self._consecutive = 0

    def record_failure(self, err: Any = None) -> None:
        """一次调用（含内部重试）最终失败记 1 次；连续达阈值 → 开闸。"""
        if self._open:
            return
        self._consecutive += 1
        if self._consecutive >= self.threshold:
            self._open = True
            self._reason = f"{type(err).__name__ if err else 'Error'}: {err}"
            self._reason = self._reason[:200]
            self._opened_at = time.strftime("%H:%M:%S")
            agent_logger.warning(
                f"[LLM::breaker] 熔断打开：连续 {self._consecutive} 次调用失败"
                f"（最近: {self._reason}）| 本 run 停用 LLM，确定性链路全速继续 | {self._opened_at}"
            )

    # ── 复位（run 级） ──
    def reset(self) -> None:
        self._consecutive = 0
        self._open = False
        self._reason = ""
        self._opened_at = ""

    def status(self) -> dict:
        return {
            "open": self._open,
            "consecutive_failures": self._consecutive,
            "threshold": self.threshold,
            "reason": self._reason,
            "opened_at": self._opened_at,
        }


# ── 全局单例：chat_json / TrackedLLM 两个 LLM 入口共用 ──
llm_breaker = LLMCircuitBreaker(threshold=getattr(config, "LLM_BREAKER_THRESHOLD", 3))
