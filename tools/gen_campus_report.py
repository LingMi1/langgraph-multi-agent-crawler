"""tools/gen_campus_report.py — 校招证据报告生成器（离线，不联网）

从已落盘的运行证据（output/）生成可摆上桌的量化报告，回答面试官三个问题：
  1. "你的 golden 指标是多少分？"      → 对 golden 清单中已有落盘的站点跑 P/R/F1
  2. "你真的跑过吗？跑了几站？"        → 实地站点统计（保存量/栏目数/轨迹数）
  3. "LLM 评估循环真的工作吗？"        → 从 trace 提取"调整前 vs 调整后"决策链

用法:
  python tools/gen_campus_report.py
输出:
  reports/campus_report.json   结构化数据（可再喂给 compare_runs）
  reports/campus_report.md     人读摘要（面试/README 引用）
"""

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.golden_check import GOLDEN_SITES, _offline_one  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
REPORT_DIR = os.path.join(PROJECT_ROOT, "reports")


# ============================================================================
# 1. 实地站点统计（保存量 / 栏目 / 轨迹）
# ============================================================================

def _scan_site(site_dir: str) -> dict:
    html = 0
    sections = set()
    for root, dirs, files in os.walk(site_dir):
        for d in dirs:
            if d != "traces":
                sections.add(d)
        html += sum(1 for f in files if f.endswith(".html"))
    traces = glob.glob(os.path.join(site_dir, "traces", "trace_*.jsonl"))
    return {"saved_html": html, "sections": sorted(sections), "traces": len(traces)}


def _scan_all_sites() -> list:
    sites = []
    for name in sorted(os.listdir(OUTPUT_DIR)):
        d = os.path.join(OUTPUT_DIR, name)
        if not os.path.isdir(d):
            continue
        stat = _scan_site(d)
        sites.append({"domain": name, **stat})
    return sites


# ============================================================================
# 2. trace 决策链提取（调整前 vs 调整后）
# ============================================================================

def _parse_trace(path: str) -> dict:
    """从一条 JSONL 轨迹提取评估循环证据。"""
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    run_id = events[0].get("run_id", "")
    seed = ""
    fetch_chain = []      # 每轮 fetch_extract 结束的 (fetched, saved)
    reviews = []          # 每轮 evaluate 审查结论
    adjustments = 0
    code_gen = False
    react = ""

    for ev in events:
        agent = ev.get("agent", "")
        event = ev.get("event", "")
        if ev.get("event") == "session_start":
            continue
        if agent == "scout" and event == "start":
            seed = ev.get("url", "")
        elif agent == "fetch_extract" and event == "end":
            decision = ev.get("decision", "")
            # decision 形如 "fetched=29 saved=6 failed=0"
            kv = {}
            for part in decision.split():
                if "=" in part:
                    k, v = part.split("=", 1)
                    kv[k] = int(v) if v.isdigit() else v
            if kv:
                fetch_chain.append({"fetched": kv.get("fetched", 0),
                                    "saved": kv.get("saved", 0)})
        elif agent == "evaluate" and event == "review":
            reviews.append({
                "score": ev.get("score"),
                "passed": ev.get("passed"),
                "issue_types": ev.get("issue_types", [])[:3],
            })
        elif agent == "config_adjust" and event == "adjust":
            adjustments = ev.get("adjustment_count", adjustments)
        elif agent == "code_gen" and event == "generate":
            code_gen = True
        elif agent == "react" and event == "end":
            react = ev.get("decision", "")

    return {
        "run_id": run_id,
        "seed": seed,
        "fetch_chain": fetch_chain,
        "reviews": reviews,
        "adjustments": adjustments,
        "code_gen": code_gen,
        "react": react,
    }


def _collect_runs() -> list:
    runs = []
    for trace_file in sorted(glob.glob(os.path.join(OUTPUT_DIR, "**", "traces",
                                                    "trace_*.jsonl"), recursive=True)):
        parsed = _parse_trace(trace_file)
        if parsed["seed"]:
            runs.append(parsed)
    return runs


# ============================================================================
# 3. 汇总报告
# ============================================================================

def _adjust_evidence(runs: list) -> dict:
    """从全量轨迹汇总'评估→调整→重抓'闭环统计。"""
    total_adjust = sum(r["adjustments"] for r in runs)
    loops = []
    for r in runs:
        if r["adjustments"] > 0:
            loop = {
                "site": r["seed"][:60],
                "rounds": len(r["fetch_chain"]),
                "adjustments": r["adjustments"],
                "saved_first": r["fetch_chain"][0]["saved"] if r["fetch_chain"] else 0,
                "saved_last": r["fetch_chain"][-1]["saved"] if r["fetch_chain"] else 0,
                "score_first": r["reviews"][0]["score"] if r["reviews"] else None,
                "score_last": r["reviews"][-1]["score"] if r["reviews"] else None,
                "code_gen": r["code_gen"],
                "react": r["react"],
            }
            loops.append(loop)
    return {"total_adjust_events": total_adjust, "runs_with_loops": len(loops), "loops": loops}


def _golden_results() -> list:
    """对 golden 清单中已有落盘的站点跑离线 P/R/F1（不联网）。"""
    results = []
    for site in GOLDEN_SITES:
        netloc = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(site["url"]).netloc.replace(":", "_")
        if os.path.isdir(os.path.join(OUTPUT_DIR, netloc)):
            results.append(_offline_one(site))
    return results


def _build_report() -> dict:
    sites = _scan_all_sites()
    runs = _collect_runs()
    return {
        "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "offline": True,
        "golden": {
            "total_sites": len(GOLDEN_SITES),
            "with_disk_evidence": len(_golden_results()),
            "results": _golden_results(),
        },
        "field_sites": {
            "count": len(sites),
            "total_saved_html": sum(s["saved_html"] for s in sites),
            "sites": sites,
        },
        "traces": {
            "count": len(runs),
            "evaluation_loops": _adjust_evidence(runs),
        },
    }


# ============================================================================
# 4. 输出
# ============================================================================

def _md(report: dict) -> str:
    L = []
    L.append("# 校招证据报告（离线生成）\n")
    L.append(f"- 生成时间: {report['generated_at']}")
    L.append(f"- 全部指标由本地已落盘产物离线计算，**不联网、不调用 LLM**，可复现。\n")

    # Golden
    g = report["golden"]
    L.append("## 1. Golden Set 离线评估（P/R/F1）\n")
    L.append(f"Golden 清单共 {g['total_sites']} 站，其中已有落盘证据、可出指标 {g['with_disk_evidence']} 站（其余站点的爬取可在联网时补跑）。\n")
    L.append("| 站点 | 保存量 | 期望 | 关键词 | P | R | F1 | 栏目发现率 | 判定 |")
    L.append("|------|-------:|-----:|:-----:|--:|--:|---:|:--:|:--:|")
    for r in g["results"]:
        m = r["metrics"]
        section = f"{m['section_recall']:.0%}" if m.get("section_recall") is not None else "-"
        L.append("| %s | %d | %d | %s | %.2f | %.2f | %.2f | %s | %s |"
                 % (r["name"], r["saved"], r["min_saved"], "✓" if r["keyword_hit"] else "✗",
                    m["precision"], m["recall"], m["f1"], section, "PASS" if r["ok"] else "FAIL"))
    L.append("")

    # 实地站点
    fs = report["field_sites"]
    L.append("## 2. 实地运行统计（真实站点）\n")
    L.append(f"共 {fs['count']} 个真实站点，累计落盘 {fs['total_saved_html']} 个 HTML 页面。\n")
    L.append("| 域名 | 保存 HTML | 栏目数 | 轨迹数 |")
    L.append("|------|---------:|-------:|-------:|")
    for s in fs["sites"]:
        L.append("| %s | %d | %d | %d |" % (s["domain"], s["saved_html"], len(s["sections"]), s["traces"]))
    L.append("")

    # 评估循环
    tr = report["traces"]
    L.append("## 3. LLM 评估循环证据（调整前 vs 调整后）\n")
    L.append(f"共 {tr['count']} 条运行轨迹，其中 {tr['evaluation_loops']['runs_with_loops']} 次运行触发了'评估→调整→重抓'闭环，累计 {tr['evaluation_loops']['total_adjust_events']} 次配置调整。\n")
    L.append("| 站点 | 轮次 | 调整次数 | saved 变化 | 评分变化 | 规则生成 | 接管 |")
    L.append("|------|-----:|-------:|:--:|:--:|:--:|:--:|")
    for lp in tr["evaluation_loops"]["loops"]:
        L.append("| %s | %d | %d | %d→%d | %s→%s | %s | %s |"
                 % (lp["site"][:34], lp["rounds"], lp["adjustments"],
                    lp["saved_first"], lp["saved_last"],
                    "-" if lp["score_first"] is None else lp["score_first"],
                    "-" if lp["score_last"] is None else lp["score_last"],
                    "✓" if lp["code_gen"] else "-",
                    lp["react"] or "-"))
    L.append("")
    L.append("> 说明：Token 成本由 `agents/budget.TrackedLLM` 在**运行时内存**记账（每次 run_crawler "
             "汇总打印 `💰 Token预算`），进程退出后无法回溯历史数值；联网重跑 `tools/golden_check.py "
             "hnbn666` 即可实时导出当次成本。")
    L.append("")
    L.append("## 4. 复现命令\n")
    L.append("```bash")
    L.append("python tools/golden_check.py hnbn666 --offline   # 单站 golden 离线复核")
    L.append("python tools/golden_check.py --list               # 查看 golden 清单")
    L.append("python tools/gen_campus_report.py                 # 重新生成本报告")
    L.append("```")
    return "\n".join(L)


def main() -> int:
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = _build_report()

    with open(os.path.join(REPORT_DIR, "campus_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(REPORT_DIR, "campus_report.md"), "w", encoding="utf-8") as f:
        f.write(_md(report))

    print("[gen_campus_report] 已生成:")
    print("  reports/campus_report.json")
    print("  reports/campus_report.md")
    print("  golden 有落盘站点: %d/%d | 实地站点: %d | 轨迹: %d"
          % (len(report["golden"]["results"]), report["golden"]["total_sites"],
             report["field_sites"]["count"], report["traces"]["count"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
