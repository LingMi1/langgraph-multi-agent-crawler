"""tests/test_plan_and_base.py — Plan-and-Execute 计划函数 + BaseAgent 模板方法测试。

覆盖：
  - _derive_plan / _review_plan（graph/agents.py）：任务计划推导与完成度审查
  - BaseAgent.run（agents/base.py）：轨迹记录、异常隔离、决策摘要
"""

import asyncio
import json
import os

from agents.base import AgentContext, BaseAgent, TraceRecorder
from graph.agents import _derive_plan, _review_plan


# ============================================================================
# Plan-and-Execute
# ============================================================================

def test_derive_plan_fields():
    plan = _derive_plan(
        {"site_type": "portal", "needs_js_render": True, "template_hints": ["rzq"]},
        ["公司简介", "产品展示"],
    )
    assert plan["status"] == "planned"
    assert plan["steps"] == ["scout", "navigate", "fetch_extract", "evaluate", "media", "storage"]
    assert plan["site_type"] == "portal"
    assert plan["needs_js_render"] is True
    assert plan["template_hints"] == ["rzq"]
    assert plan["expected_sections"] == ["公司简介", "产品展示"]


def test_review_plan_all_steps_done():
    plan = _derive_plan({}, [])
    review = _review_plan(
        plan,
        {"passed": True},
        {"scouted": 1, "fetched": 5, "saved": 4},
    )
    assert review["pending"] == []
    assert review["passed"] is True
    assert review["completed"] == plan["steps"]


def test_review_plan_partial_pending():
    plan = _derive_plan({}, [])
    review = _review_plan(
        plan,
        {"passed": False, "issues": [{"type": "empty_shell"}]},
        {"scouted": 1},
    )
    assert "scout" in review["completed"]
    assert "storage" in review["pending"]
    assert review["passed"] is False


def test_review_plan_quality_gap_joined():
    plan = _derive_plan({}, [])
    review = _review_plan(
        plan,
        {"passed": False, "issues": [{"type": "a"}, {"type": "b"}]},
        {},
    )
    assert review["quality_gap"] == "a;b"


def test_review_plan_without_evaluation_no_crash():
    # 无评估结果 → evaluate 步骤未完成保持 pending，passed=False，不崩溃
    plan = _derive_plan({}, [])
    review = _review_plan(plan, None, {"scouted": 1, "fetched": 1, "saved": 1})
    assert review["passed"] is False
    assert "evaluate" in review["pending"]
    assert review["quality_gap"] == ""


# ============================================================================
# BaseAgent 模板方法
# ============================================================================

class DummyAgent(BaseAgent):
    name = "dummy"
    role = "测试"

    async def run_impl(self, state):
        return {"ok": True, "url": state.get("seed_url")}


class BoomAgent(BaseAgent):
    name = "boom"

    async def run_impl(self, state):
        raise RuntimeError("boom")


class DecideAgent(BaseAgent):
    name = "decide"

    async def run_impl(self, state):
        return {"ok": True}

    def _summarize_decision(self, result):
        return f"ok={result.get('ok')}"


def _read_events(trace: TraceRecorder):
    if not trace.path or not os.path.exists(trace.path):
        return []
    with open(trace.path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_base_agent_records_start_and_end(tmp_path):
    trace = TraceRecorder(output_dir=str(tmp_path), run_id="t1")
    ctx = AgentContext(trace=trace)
    agent = DummyAgent(ctx)

    result = asyncio.run(agent.run({"seed_url": "http://x.cn", "queue": [1, 2]}))

    assert result == {"ok": True, "url": "http://x.cn"}
    events = _read_events(trace)
    ev = [e for e in events if e.get("agent") == "dummy"]
    assert ev[0]["event"] == "start"
    assert ev[0]["url"] == "http://x.cn"
    assert ev[0]["queue"] == 2
    assert ev[-1]["event"] == "end"
    assert "ok" in ev[-1]["keys"]


def test_base_agent_exception_isolation(tmp_path):
    """异常隔离：Agent 抛错 → 降级返回 error，不向上传播，trace 记 error。"""
    trace = TraceRecorder(output_dir=str(tmp_path), run_id="t2")
    agent = BoomAgent(AgentContext(trace=trace))

    result = asyncio.run(agent.run({"seed_url": "http://x.cn"}))

    assert "error" in result
    assert "boom" in result["error"]
    events = _read_events(trace)
    err = [e for e in events if e["event"] == "error"]
    assert len(err) == 1
    assert "boom" in err[0]["error"]


def test_base_agent_none_result_tolerated(tmp_path):
    """run_impl 返回 None → 视为空 dict，不抛异常。"""
    class NoneAgent(BaseAgent):
        name = "none_agent"

        async def run_impl(self, state):
            return None

    trace = TraceRecorder(output_dir=str(tmp_path), run_id="t3")
    agent = NoneAgent(AgentContext(trace=trace))
    result = asyncio.run(agent.run({}))
    assert result == {}


def test_base_agent_decision_summary_in_trace(tmp_path):
    trace = TraceRecorder(output_dir=str(tmp_path), run_id="t4")
    agent = DecideAgent(AgentContext(trace=trace))
    asyncio.run(agent.run({}))
    events = _read_events(trace)
    end = [e for e in events if e["event"] == "end"][0]
    assert end["decision"] == "ok=True"


def test_base_agent_requires_run_impl():
    class Incomplete(BaseAgent):
        name = "incomplete"

    try:
        Incomplete(AgentContext())
    except TypeError:
        pass  # 抽象方法未实现 → 不可实例化
    else:
        raise AssertionError("未实现 run_impl 的子类不应可实例化")
