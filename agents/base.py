"""
agents/base.py — 编排级 Agent 抽象层（Supervisor 多智能体架构）

分层模型：
  1. 编排级 Agent（本文件 + graph/agents.py）：LangGraph 图中每个节点 = 一个 Agent
     - 有独立职责（role）、行为描述（description）、system_prompt
     - 每次 run 都被 TraceRecorder 记录为一条轨迹（决策可复现、可调试）
     - 异常被隔离在 Agent 边界内：返回 {"error": ...} 降级，由 Supervisor 路由兜底，
       不打断整图
  2. 能力级 Agent（agents/scout.py nav.py fetcher.py extractor.py storage.py）：
     单页级工具能力，供编排级 Agent 调用

架构叙事（面试）：
  - Supervisor 模式：graph/workflow.py 是监督者，按图编排子 Agent；EvaluateAgent
    是审查者，决定任务交给 AdjustAgent / CodeGenAgent 还是放行存储
  - Plan-and-Execute：ScoutAgent 产出任务计划（plan），EvaluateAgent 对照计划
    检查完成度并写入 trace
  - 确定性优先：Agent 内部以确定性规则为主，LLM 仅在 Evaluate/CodeGen 等
    关键节点介入（成本分层）
  - 全程可观测：TraceRecorder 把每个 Agent 的入参摘要 / 决策 / 出参 / 耗时
    落盘为 JSONL
"""

from __future__ import annotations

import json
import os
import time
import traceback as _tb
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from schemas import agent_logger
from memory import UrlMemory


# ============================================================================
# TraceRecorder — 多 Agent 轨迹记录器（JSONL）
# ============================================================================

class TraceRecorder:
    """把多 Agent 执行轨迹落盘为 JSONL，便于复盘与调试。

    每个事件一行 JSON：
      {"ts": "...", "run_id": "...", "seq": 1, "agent": "scout", "event": "start", ...}

    事件类型：
      session_start / start / end / error / decision / review / plan
    """

    def __init__(self, output_dir: str = "", run_id: Optional[str] = None):
        self.run_id = run_id or time.strftime("%Y%m%d_%H%M%S")
        self._seq = 0
        self._path = ""
        if output_dir:
            traces_dir = os.path.join(output_dir, "traces")
            try:
                os.makedirs(traces_dir, exist_ok=True)
            except Exception as e:
                agent_logger.warning(f"[Trace] 创建 traces 目录失败: {e}")
            else:
                self._path = os.path.join(traces_dir, f"trace_{self.run_id}.jsonl")
                self._write({
                    "event": "session_start",
                    "run_id": self.run_id,
                    "cwd": os.getcwd(),
                })

    @property
    def path(self) -> str:
        return self._path

    def _write(self, payload: Dict[str, Any]) -> None:
        if not self._path:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            agent_logger.warning(f"[Trace] 写入失败: {e}")

    def record(self, agent: str, event: str, **payload: Any) -> None:
        """记录一条 Agent 轨迹事件。"""
        self._seq += 1
        self._write({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "run_id": self.run_id,
            "seq": self._seq,
            "agent": agent,
            "event": event,
            **payload,
        })


# ============================================================================
# AgentContext — Agent 运行时上下文
# ============================================================================

class AgentContext:
    """Agent 运行时共享上下文：轨迹记录器 + 可选 LLM 客户端 + 共享记忆。"""

    def __init__(
        self,
        trace: Optional[TraceRecorder] = None,
        llm: Any = None,
        memory: Optional[UrlMemory] = None,
    ):
        self.trace = trace or TraceRecorder()
        self.llm = llm
        self.memory = memory or UrlMemory()


# ============================================================================
# BaseAgent — 编排级 Agent 基类
# ============================================================================

class BaseAgent(ABC):
    """编排级 Agent 基类。

    子类只需实现 run_impl()（通常直接调用现有节点函数），职责声明与
    trace 记录 / 异常隔离由基类统一完成。
    """

    # ── 职责声明（面试叙事 / 可观测用） ──
    name: str = "agent"
    role: str = ""
    description: str = ""
    system_prompt: str = ""

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx
        self.trace = ctx.trace

    # ── 模板方法：trace + 异常隔离 + 耗时统计 ──
    async def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Agent 统一入口：记录轨迹、隔离异常、统计耗时。"""
        start = time.perf_counter()
        self.trace.record(
            self.name, "start",
            url=str(state.get("seed_url", ""))[:80],
            queue=len(state.get("queue", [])),
        )
        try:
            result = await self.run_impl(state)
            if result is None:
                result = {}
            cost_ms = round((time.perf_counter() - start) * 1000, 1)
            self.trace.record(
                self.name, "end",
                ms=cost_ms,
                keys=list(result.keys()),
                error=str(result.get("error", ""))[:200],
                decision=self._summarize_decision(result),
            )
            return result
        except Exception as e:
            cost_ms = round((time.perf_counter() - start) * 1000, 1)
            agent_logger.error(
                f"[Agent::{self.name}] 异常: {e}\n{_tb.format_exc()}"
            )
            self.trace.record(self.name, "error", ms=cost_ms, error=str(e))
            # 异常隔离：降级返回 error，由 Supervisor 路由兜底，不打断整图
            return {"error": f"[{self.name}] {e}"}

    # ── 子类实现 ──
    @abstractmethod
    async def run_impl(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """执行 Agent 职责，返回 state 增量 dict。"""

    # ── 决策摘要（子类可覆写，写入 trace 的 decision 字段） ──
    def _summarize_decision(self, result: Dict[str, Any]) -> str:
        return ""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.__class__.__name__} name={self.name!r} role={self.role!r}>"
