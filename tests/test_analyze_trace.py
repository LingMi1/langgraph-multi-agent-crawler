"""tests/test_analyze_trace.py — 轨迹分析器（token/成本统计 + Agent 成功率）单元测试。

覆盖 tools/analyze_trace.py：结构化汇总、显式成本记账优先、启发式成本估算兜底、
Agent 成功率口径。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tools.analyze_trace import _collect_explicit_cost, _estimate_cost, summarize

BASE = {
    "run_id": "test_run",
    "seq": 1,
    "ts": "2026-08-31 10:00:00",
}


def _write_trace(tmp_path, lines):
    p = tmp_path / "trace_test.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return str(p)


@pytest.fixture
def trace_path(tmp_path):
    lines = [
        {**BASE, "agent": "scout", "event": "start", "url": "http://example.com/"},
        {**BASE, "agent": "scout", "event": "end", "ms": 120.0, "decision": "site_type=portal"},
        {**BASE, "agent": "evaluate", "event": "start", "url": "http://example.com/"},
        {**BASE, "agent": "evaluate", "event": "end", "ms": 800.0, "decision": "plan_ok"},
        {**BASE, "agent": "evaluate", "event": "error", "ms": 50.0, "error": "llm timeout"},
        {**BASE, "event": "session_start", "ts": "2026-08-31 10:00:01"},
    ]
    return _write_trace(tmp_path, lines)


class TestSummarize:
    def test_empty_trace(self, tmp_path):
        p = _write_trace(tmp_path, [])
        assert summarize(p)["empty"] is True

    def test_overview_counts(self, trace_path):
        s = summarize(trace_path)
        assert s["total_events"] == 6
        assert s["starts"] == 2
        assert s["ends"] == 2
        assert s["errors"] == 1

    def test_agent_distribution(self, trace_path):
        s = summarize(trace_path)
        assert s["agents"]["scout"]["calls"] == 1
        assert s["agents"]["scout"]["avg_ms"] == 120.0
        assert s["agents"]["evaluate"]["errors"] == 1
        assert s["agents"]["evaluate"]["max_ms"] == 800.0

    def test_agent_success_rate(self, trace_path):
        """成功率 = 调用(end) / (调用+错误)，即 2/3。"""
        s = summarize(trace_path)
        assert s["agent_success_rate"] == pytest.approx(2 / 3, abs=0.001)

    def test_event_dist(self, trace_path):
        s = summarize(trace_path)
        assert s["event_dist"]["start"] == 2
        assert s["event_dist"]["end"] == 2
        assert s["event_dist"]["error"] == 1


class TestCost:
    def test_explicit_cost_preferred(self, trace_path):
        s = summarize(trace_path)
        # 该轨迹无显式 token 字段 → 走启发式估算
        assert s["llm_cost"]["source"] == "estimate"

    def test_explicit_cost_fields(self, tmp_path):
        lines = [
            {**BASE, "agent": "evaluate", "event": "end",
             "prompt_tokens": 100, "completion_tokens": 30, "cost": 0.01},
            {**BASE, "agent": "code_gen", "event": "end",
             "prompt_tokens": 200, "completion_tokens": 50, "cost": 0.02},
        ]
        p = _write_trace(tmp_path, lines)
        s = summarize(p)
        c = s["llm_cost"]
        assert c["source"] == "explicit"
        assert c["prompt_tokens"] == 300
        assert c["completion_tokens"] == 80
        assert c["cost"] == pytest.approx(0.03)
        assert c["calls"] == 2

    def test_estimate_cost_nonzero(self, tmp_path):
        lines = [
            {**BASE, "agent": "evaluate", "event": "end",
             "decision": "检测到产品详情页，正文质量达标，计划完成度 100%"},
        ]
        p = _write_trace(tmp_path, lines)
        c = summarize(p)["llm_cost"]
        assert c["source"] == "estimate"
        assert c["prompt_tokens"] > 0
        assert c["cost"] > 0

    def test_collect_explicit_skips_non_cost(self, trace_path):
        events = [json.loads(l) for l in open(trace_path, encoding="utf-8") if l.strip()]
        t = _collect_explicit_cost(events)
        assert t["calls"] == 0


class TestMain:
    def test_main_json_flag(self, tmp_path, capsys):
        from tools.analyze_trace import main

        lines = [
            {**BASE, "agent": "scout", "event": "start"},
            {**BASE, "agent": "scout", "event": "end", "ms": 10.0},
        ]
        p = _write_trace(tmp_path, lines)
        assert main([p, "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["total_events"] == 2
        assert out["agent_success_rate"] == 1.0

    def test_main_missing_file(self):
        from tools.analyze_trace import main

        assert main(["no_such_trace.jsonl"]) == 2
