"""tools/analyze_trace.py — 多 Agent 轨迹分析器

读取 TraceRecorder 落盘的 JSONL 轨迹，输出统计：
  - 概览（事件数 / 起止时间 / 总时长）
  - Agent 调用分布（调用次数 / 平均与最大耗时 / 错误数）+ Agent 成功率
  - 事件类型分布
  - 错误明细
  - LLM token / 成本估算：
      * 事件若显式携带 prompt_tokens / completion_tokens / cost 则直接汇总
      * 否则对轨迹中的文本载荷（decision / plan / error / url 等）用
        estimate_tokens 做启发式估算（口径与 agents/budget.py 一致，
        仅用于成本量级评估，不追求精确计数）

用法:
  python tools/analyze_trace.py output/<netloc>/traces/trace_<ts>.jsonl
  python tools/analyze_trace.py output/www.hnbn666.cn/traces/trace_20260826_143504.jsonl
  python tools/analyze_trace.py --json output/.../trace_*.jsonl   # 结构化输出（CI 采集）
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.budget import estimate_tokens  # noqa: E402

# 成本估算默认单价（$ / 1K token，DeepSeek 级别量级，仅估算用）
INPUT_PRICE_PER_1K = 0.00014
OUTPUT_PRICE_PER_1K = 0.00028

# 轨迹中可能带文本、可作为 token 估算来源的载荷字段
_TEXT_FIELDS = ("decision", "plan", "error", "url", "prompt", "content", "output")


def load_events(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _collect_explicit_cost(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """事件若显式携带 token/成本字段则直接汇总（向前兼容更细的插桩）。"""
    t = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    for e in events:
        pt = e.get("prompt_tokens")
        ct = e.get("completion_tokens")
        cst = e.get("cost")
        if any(v is not None for v in (pt, ct, cst)):
            t["calls"] += 1
            t["prompt_tokens"] += int(pt or 0)
            t["completion_tokens"] += int(ct or 0)
            t["cost"] += float(cst or 0.0)
    return t


def _estimate_cost(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对轨迹文本载荷做启发式 token 估算 + 成本量级评估。

    与显式记账的分工：轨迹文本只是"决策摘要"，token 量级远小于真实调用，
    所以输出明确标注 source=estimate，仅作量级参考。
    """
    texts = {"prompt": [], "completion": []}
    for e in events:
        for f in _TEXT_FIELDS:
            v = e.get(f)
            if not isinstance(v, str) or not v:
                continue
            if f in ("output", "content"):
                texts["completion"].append(v)
            else:
                texts["prompt"].append(v)
    prompt_tokens = sum(estimate_tokens(t) for t in texts["prompt"])
    completion_tokens = sum(estimate_tokens(t) for t in texts["completion"])
    cost = prompt_tokens / 1000 * INPUT_PRICE_PER_1K + completion_tokens / 1000 * OUTPUT_PRICE_PER_1K
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost": round(cost, 6),
        "source": "estimate",
    }


def summarize(path: str) -> Dict[str, Any]:
    """结构化汇总一条轨迹（CI 可采集），analyze() 负责打印。"""
    events = load_events(path)
    if not events:
        return {"path": path, "empty": True}

    starts = [e for e in events if e.get("event") == "start"]
    ends = [e for e in events if e.get("event") == "end"]
    errors = [e for e in events if e.get("event") == "error"]

    # Agent 调用分布
    agent_ms: Dict[str, List[float]] = defaultdict(list)
    agent_err: Counter = Counter()
    agent_calls: Counter = Counter()
    for e in events:
        agent = e.get("agent") or "?"
        if e.get("event") == "end":
            agent_calls[agent] += 1
            ms = e.get("ms")
            if ms is not None:
                agent_ms[agent].append(float(ms))
        elif e.get("event") == "error":
            agent_err[agent] += 1

    total_calls = sum(agent_calls.values())
    total_errors = sum(agent_err.values())
    agents = {}
    for agent in sorted(set(list(agent_calls) + list(agent_err))):
        ms = agent_ms.get(agent, [])
        agents[agent] = {
            "calls": agent_calls[agent],
            "errors": agent_err[agent],
            "avg_ms": round(sum(ms) / len(ms), 1) if ms else 0.0,
            "max_ms": max(ms) if ms else 0.0,
        }

    ts = [e.get("ts", "") for e in events if e.get("ts")]
    span_s = None
    if len(ts) >= 2:
        try:
            span_s = round((datetime.fromisoformat(ts[-1]) - datetime.fromisoformat(ts[0])).total_seconds(), 1)
        except ValueError:
            span_s = None

    # LLM 成本：显式记账优先，否则启发式估算
    explicit = _collect_explicit_cost(events)
    if explicit["calls"]:
        cost = explicit | {"source": "explicit"}
    else:
        cost = _estimate_cost(events)

    return {
        "path": path,
        "empty": False,
        "total_events": len(events),
        "starts": len(starts),
        "ends": len(ends),
        "errors": len(errors),
        "span_s": span_s,
        "first_ts": ts[0] if ts else "",
        "last_ts": ts[-1] if ts else "",
        "agents": agents,
        "event_dist": dict(Counter(e.get("event") for e in events).most_common()),
        "agent_success_rate": round(total_calls / (total_calls + total_errors), 4)
        if (total_calls + total_errors) else 1.0,
        "llm_cost": cost,
        "errors_detail": [
            {"agent": e.get("agent"), "error": str(e.get("error", ""))[:120]}
            for e in errors[:5]
        ],
    }


def analyze(path: str) -> None:
    s = summarize(path)
    if s.get("empty"):
        print("轨迹为空")
        return

    print("=" * 66)
    print(f"轨迹文件 : {s['path']}")
    print(f"事件总数 : {s['total_events']} | start={s['starts']} end={s['ends']} "
          f"error={s['errors']}")
    if s.get("span_s") is not None:
        print(f"时间跨度 : {s['first_ts']} → {s['last_ts']} ({s['span_s']}s)")

    print("-" * 66)
    print(f"{'Agent':<18}{'调用':>5}{'错误':>5}{'平均ms':>9}{'最大ms':>9}")
    print("-" * 66)
    for agent, a in s["agents"].items():
        print(f"{agent:<18}{a['calls']:>5}{a['errors']:>5}"
              f"{a['avg_ms']:>9.0f}{a['max_ms']:>9.0f}")

    # ── Agent 成功率（任务成功率的事件层证据） ──
    rate = s["agent_success_rate"]
    print("-" * 66)
    print(f"Agent 成功率 : {rate:.1%}（调用={s['ends']} 错误={s['errors']}）")

    # ── LLM token / 成本 ──
    c = s["llm_cost"]
    src = {"explicit": "显式记账", "estimate": "启发式估算（轨迹文本）", "none": "无"}[c["source"]]
    print("-" * 66)
    print(f"LLM 成本({src}): prompt≈{c['prompt_tokens']}tok "
          f"completion≈{c['completion_tokens']}tok cost≈${c['cost']:.6f}")

    # ── 决策摘要（每个 Agent 最多 2 条）──
    print("-" * 66)
    print("决策摘要（前 2 条 / Agent）:")
    seen: Dict[str, int] = defaultdict(int)
    for e in load_events(path):
        agent = e.get("agent") or "?"
        d = e.get("decision")
        if d and seen[agent] < 2:
            seen[agent] += 1
            print(f"  [{agent}] {str(d)[:60]}")

    # ── 事件类型分布 ──
    print("-" * 66)
    print("事件类型分布:", dict(s["event_dist"]))

    # ── 错误明细 ──
    if s["errors_detail"]:
        print("-" * 66)
        print("错误明细:")
        for d in s["errors_detail"]:
            print(f"  [{d['agent']}] {d['error']}")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="多 Agent 轨迹分析器")
    parser.add_argument("path", help="trace_*.jsonl 轨迹文件路径")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON（CI 采集）")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.path):
        print(f"轨迹文件不存在: {args.path}")
        return 2
    if args.json:
        print(json.dumps(summarize(args.path), ensure_ascii=False, indent=2))
    else:
        analyze(args.path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
