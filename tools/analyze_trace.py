"""tools/analyze_trace.py — 多 Agent 轨迹分析器

读取 TraceRecorder 落盘的 JSONL 轨迹，输出统计：
  - 概览（事件数 / 起止时间 / 总时长）
  - Agent 调用分布（调用次数 / 平均与最大耗时 / 错误数）
  - 事件类型分布
  - 错误明细

用法:
  python tools/analyze_trace.py output/<netloc>/traces/trace_<ts>.jsonl
  python tools/analyze_trace.py output/www.hnbn666.cn/traces/trace_20260826_143504.jsonl
"""

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_events(path: str):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def analyze(path: str) -> None:
    events = load_events(path)
    if not events:
        print("轨迹为空")
        return

    # ── 概览 ──
    starts = [e for e in events if e.get("event") == "start"]
    ends = [e for e in events if e.get("event") == "end"]
    errors = [e for e in events if e.get("event") == "error"]
    print("=" * 66)
    print(f"轨迹文件 : {path}")
    print(f"事件总数 : {len(events)} | start={len(starts)} end={len(ends)} error={len(errors)}")
    ts = [e.get("ts", "") for e in events if e.get("ts")]
    if ts:
        try:
            span = datetime.fromisoformat(ts[-1]) - datetime.fromisoformat(ts[0])
            print(f"时间跨度 : {ts[0]} → {ts[-1]} ({span.total_seconds():.1f}s)")
        except ValueError:
            print(f"时间跨度 : {ts[0]} → {ts[-1]}")

    # ── Agent 调用分布 ──
    agent_ms = defaultdict(list)
    agent_err = Counter()
    agent_end = Counter()
    agent_decisions = defaultdict(list)
    for e in events:
        agent = e.get("agent") or "?"
        if e.get("event") == "end":
            agent_end[agent] += 1
            ms = e.get("ms")
            if ms is not None:
                agent_ms[agent].append(ms)
            decision = e.get("decision")
            if decision:
                agent_decisions[agent].append(str(decision)[:60])
        elif e.get("event") == "error":
            agent_err[agent] += 1

    print("-" * 66)
    print(f"{'Agent':<18}{'调用':>5}{'错误':>5}{'平均ms':>9}{'最大ms':>9}")
    print("-" * 66)
    for agent in sorted(set(list(agent_end) + list(agent_err))):
        ms = agent_ms.get(agent, [])
        avg = sum(ms) / len(ms) if ms else 0
        mx = max(ms) if ms else 0
        print(f"{agent:<18}{agent_end[agent]:>5}{agent_err[agent]:>5}"
              f"{avg:>9.0f}{mx:>9.0f}")

    # ── 决策摘要（每个 Agent 最多 2 条）──
    print("-" * 66)
    print("决策摘要（前 2 条 / Agent）:")
    for agent, decs in agent_decisions.items():
        for d in decs[:2]:
            print(f"  [{agent}] {d}")

    # ── 事件类型分布 ──
    print("-" * 66)
    dist = Counter(e.get("event") for e in events)
    print("事件类型分布:", dict(dist.most_common()))

    # ── 错误明细 ──
    if errors:
        print("-" * 66)
        print("错误明细:")
        for e in errors[:5]:
            print(f"  [{e.get('agent')}] {str(e.get('error', ''))[:120]}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    analyze(sys.argv[1])
