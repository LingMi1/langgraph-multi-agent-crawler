"""tools/golden_check.py — 离线评估集（Golden Set）回归检查

对一组"已知答案"的站点跑完整爬取，断言关键指标（保存量 / 落盘关键词 /
P/R/F1 覆盖率 / 栏目发现率），用于验证清洗链路与规则改动没有引入回归，
并给每次改动产出可对比的量化指标（与 tools/compare_runs.py 联动）。

指标层（召回型任务：爬虫要"尽量全、尽量准"）：
  - precision / recall / f1：保存覆盖率口径（agents/eval.compute_prf）
  - section_recall：期望栏目在落盘目录中的发现率（recall@k 思想的栏目版）
  - keyword_hit：落盘内容关键词断言
  - success（任务成功率）：硬断言全过 **且** LLM 预算达标（calls/token/成本
    快照差分，区分"完成质量"与"资源效率"两个维度）——这是 Agent 任务的
    二元成功口径，聚合后即"端到端任务成功率 X/Y"。
排序类检索指标（recall@k / ndcg@k）属于 RAG 链路（tools/rag_demo.py），
两套指标各司其职，README 有定位说明。

用法:
  python tools/golden_check.py            # 跑全部 golden 站点
  python tools/golden_check.py hnbn666   # 只跑匹配的站点
  python tools/golden_check.py --list    # 列出 golden 清单
  python tools/golden_check.py --offline # 离线复核：不联网，只对已落盘 HTML 断言
"""

import argparse
import asyncio
import os
import re
import sys
from typing import Optional
from urllib.parse import urlparse

# 保证从项目根目录可导入（config / graph / agents）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.eval import compute_prf
from config import LOCAL_BACKUP_DIR
from graph.workflow import run_crawler

# ── Golden Set：已知答案的评估站点 ──
# site_type 为站点画像期望值（记录用；run_crawler 不对外暴露 plan，故运行时
# 以 saved 数量 + 落盘关键词断言为主）。
GOLDEN_SITES = [
    {
        "name": "hnbn666",
        "url": "http://www.hnbn666.cn/001_about.html",
        "site_type": "portal",
        "min_saved": 6,
        "keyword": "豫花",          # 规则13 纯图产品页应保存（标题含产品名）
        "desc": "RuiQiCMS 可视化建站 / 纯图产品详情页（规则13）",
    },
    {
        "name": "books",
        "url": "http://books.toscrape.com/",
        "site_type": "ecommerce",
        "min_saved": 20,            # 首页即列出 20 本书，全站导航只会更多（硬断言）
        "expected_saved": 20,       # P/R/F1 分母：达到首页量即视为召回达标
        "keyword": "Books to Scrape",
        "desc": "Scrapy 教程标配公开演示站 / 电商书架模板（模板多样性回归）",
    },
    {
        "name": "quotes",
        "url": "http://quotes.toscrape.com/",
        "site_type": "paged_list",
        "min_saved": 1,             # 硬断言只要求首页可达
        "expected_saved": 10,       # 分页列表应有 10 页 → 分页发现能力以 recall 度量
        "keyword": "Quotes to Scrape",
        "desc": "Scrapy 教程标配公开演示站 / 多页分页列表模板（分页发现回归）",
    },
    # 新增站点时按此模板扩展：
    # {
    #     "name": "example",
    #     "url": "https://example.com/",
    #     "site_type": "cms",
    #     "min_saved": 3,          # 最低保存量（硬断言）
    #     "expected_saved": 5,     # 期望保存量（P/R/F1 分母，缺省取 min_saved）
    #     "keyword": "公司",        # 落盘内容关键词（可选）
    #     "expected_sections": ["关于", "产品"],  # 期望栏目（section_recall，可选）
    #     "desc": "示例站点描述",
    # },
]


def _scan_backup(site: dict) -> tuple:
    """扫描该站点已落盘的 HTML：返回 (saved, keyword_hit, discovered_sections)。

    在线 / 离线两条路径共用同一份"落盘断言"，保证判定口径一致。
    discovered_sections = 落盘目录下的顶层子目录名集合（栏目子目录）。
    """
    netloc = urlparse(site["url"]).netloc.replace(":", "_")
    out_dir = os.path.join(LOCAL_BACKUP_DIR, netloc)
    saved = 0
    keyword_hit = not site.get("keyword")
    sections = set()
    if os.path.isdir(out_dir):
        try:
            sections = {
                d for d in os.listdir(out_dir)
                if os.path.isdir(os.path.join(out_dir, d))
            }
        except OSError:
            sections = set()
        for root, _, fs in os.walk(out_dir):
            for name in fs:
                if not name.endswith(".html"):
                    continue
                saved += 1
                if keyword_hit:
                    continue
                try:
                    with open(os.path.join(root, name), encoding="utf-8", errors="ignore") as f:
                        if site["keyword"] in f.read():
                            keyword_hit = True
                except OSError:
                    continue
    return saved, keyword_hit, sections


def _metrics(site: dict, saved: int, sections: set) -> dict:
    """golden 指标层：P/R/F1（保存覆盖率）+ 栏目发现率（recall@k 思想的栏目版）。

    overlap 采用保守口径 min(saved, expected)：只把"已保存"计入正确命中，
    不臆测未落盘的页面是否正确。
    """
    expected = int(site.get("expected_saved") or site.get("min_saved") or 1)
    overlap = min(saved, expected)
    prf = compute_prf(expected, saved, overlap)
    metrics = {
        "precision": prf["precision"],
        "recall": prf["recall"],
        "f1": prf["f1"],
        "coverage": round(overlap / expected, 4),
    }
    exp_sections = site.get("expected_sections") or []
    if exp_sections:
        hit = len(set(exp_sections) & sections)
        metrics["section_recall"] = round(hit / len(exp_sections), 4)
    else:
        metrics["section_recall"] = None
    return metrics


# ── 任务成功率：预算约束（区分"完成质量"与"资源效率"） ──

DEFAULT_BUDGET_CAP = {"max_calls": 60, "max_tokens": 300_000, "max_cost": 0.5}


def _budget_delta(after: dict, before: dict) -> dict:
    """本次任务的 LLM 成本增量（快照差分，避免跨站点累计污染单任务口径）。"""
    if not after:
        return {}
    before_total = before.get("total", {}) if before else {}
    return {
        "total": {
            k: after["total"].get(k, 0) - before_total.get(k, 0)
            for k in ("calls", "prompt_tokens", "completion_tokens", "cost")
        }
    }


def _budget_within_cap(budget: dict, cap: Optional[dict]) -> tuple:
    """预算约束断言：返回 (within, reasons)。无预算数据（未配 LLM）不评分。"""
    if not budget:
        return True, []
    cap = cap or DEFAULT_BUDGET_CAP
    t = budget.get("total", {})
    within, reasons = True, []
    if t.get("calls", 0) > cap.get("max_calls", 1 << 30):
        within, reasons = False, reasons + [f"calls={t['calls']} > 上限 {cap['max_calls']}"]
    if t.get("prompt_tokens", 0) > cap.get("max_tokens", 1 << 30):
        within, reasons = False, reasons + [
            f"prompt_tokens={t['prompt_tokens']} > 上限 {cap['max_tokens']}"]
    if t.get("cost", 0) > cap.get("max_cost", float("inf")):
        within, reasons = False, reasons + [f"cost=${t['cost']:.4f} > 上限 ${cap['max_cost']}"]
    return within, reasons


def _budget_one_line(budget: dict) -> str:
    if not budget:
        return "无LLM记账"
    t = budget.get("total", {})
    return f"call={t.get('calls', 0)}/{t.get('prompt_tokens', 0)}tok/${t.get('cost', 0):.4f}"


def _run_one(site: dict, max_steps: int = 3000) -> dict:
    """跑单个 golden 站点，返回结构化评估结果（含指标、预算与耗时）。"""
    import time

    t0 = time.time()
    stats = {"saved": 0, "failed": 0}
    budget = {}
    try:
        # 预算快照差分：只统计本次任务的 LLM 成本，不跨站点累计
        from graph.nodes import get_budget_data
        before = get_budget_data()
        stats = asyncio.run(run_crawler(site["url"], max_steps=max_steps))
        budget = _budget_delta(get_budget_data(), before)
    except Exception as e:  # noqa: BLE001 —— golden 检查要吞异常并给出失败原因
        return {
            "name": site["name"], "ok": False, "success": False, "saved": 0,
            "min_saved": site["min_saved"], "keyword_hit": False,
            "reasons": [f"爬取异常: {e}"], "elapsed_s": round(time.time() - t0, 1),
        }

    saved, keyword_hit, sections = _scan_backup(site)
    metrics = _metrics(site, saved, sections)
    ok = True
    reasons = []

    if saved < site["min_saved"]:
        ok = False
        reasons.append(f"saved={saved} < 期望 {site['min_saved']}")

    if site.get("keyword") and not keyword_hit:
        ok = False
        reasons.append(f"落盘内容未找到关键词 {site['keyword']!r}")

    budget_ok, budget_reasons = _budget_within_cap(budget, site.get("budget_cap"))
    reasons += budget_reasons
    return {
        "name": site["name"], "ok": ok,
        "success": ok and budget_ok,             # 任务成功率口径：质量 + 资源效率
        "saved": saved, "min_saved": site["min_saved"],
        "keyword_hit": keyword_hit, "metrics": metrics, "reasons": reasons,
        "budget": budget, "budget_ok": budget_ok,
        "elapsed_s": round(time.time() - t0, 1),
    }


def _offline_one(site: dict) -> dict:
    """离线复核：不联网爬取，直接对已落盘的 HTML 做 golden 断言。

    面向无网络 CI / 本地快速回归：验证清洗与规则链路没有引入回归，
    但只覆盖"落盘内容"这一层，不验证抓取过程本身。
    """
    import time

    t0 = time.time()
    saved, keyword_hit, sections = _scan_backup(site)
    metrics = _metrics(site, saved, sections)
    ok = True
    reasons = []
    if saved < site["min_saved"]:
        ok = False
        reasons.append(f"saved={saved} < 期望 {site['min_saved']}（离线目录无足够落盘）")
    if site.get("keyword") and not keyword_hit:
        ok = False
        reasons.append(f"落盘内容未找到关键词 {site['keyword']!r}")

    return {
        "name": site["name"], "ok": ok, "success": ok, "saved": saved,
        "min_saved": site["min_saved"],
        "keyword_hit": keyword_hit, "metrics": metrics, "reasons": reasons,
        "budget": {}, "budget_ok": True,          # 离线不产生 LLM 成本，预算恒达标
        "elapsed_s": round(time.time() - t0, 1), "offline": True,
    }


def _format_result(r: dict) -> str:
    status = "PASS" if r["success"] else "FAIL"
    m = r.get("metrics") or {}
    detail = f"saved={r['saved']} recall={m.get('recall', '-')} f1={m.get('f1', '-')}"
    if m.get("section_recall") is not None:
        detail += f" section={m['section_recall']}"
    detail += f" budget[{_budget_one_line(r.get('budget'))}]"
    if r.get("reasons"):
        detail += " | " + "; ".join(r["reasons"])
    return f"[{status}] {r['name']:<12} {detail} ({r['elapsed_s']}s)"


def main() -> int:
    parser = argparse.ArgumentParser(description="Golden Set 离线评估")
    parser.add_argument("filter", nargs="?", default="", help="站点名过滤（正则）")
    parser.add_argument("--list", action="store_true", help="仅列出 golden 清单")
    parser.add_argument("--json", action="store_true",
                        help="输出结构化 JSON 报告（CI / 指标采集用）")
    parser.add_argument("--offline", action="store_true",
                        help="离线复核：不联网爬取，只对已落盘 HTML 做 golden 断言（无网络 CI）")
    args = parser.parse_args()

    if args.list:
        for s in GOLDEN_SITES:
            print(f"  {s['name']:<10} {s['desc']}")
        return 0

    sites = [s for s in GOLDEN_SITES if re.search(args.filter, s["name"], re.I)]
    if not sites:
        print(f"未匹配到站点: {args.filter!r}")
        return 2

    runner = _offline_one if args.offline else _run_one
    results = [runner(s) for s in sites]
    successes = sum(1 if r["success"] else 0 for r in results)
    failed = len(results) - successes

    if args.json:
        import json
        report = {
            "suite": "golden_set",
            "total": len(results),
            "passed": successes,
            "failed": failed,
            "success_rate": round(successes / len(results), 4) if results else 0.0,
            "sites": results,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(_format_result(r))
        print("-" * 60)
        rate = f"{successes / len(results):.0%}" if results else "-"
        print(f"Golden 评估完成 | 任务成功率={successes}/{len(results)} ({rate}) | 失败={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
