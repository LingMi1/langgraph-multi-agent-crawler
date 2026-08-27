"""tools/compare_runs.py — golden 两次运行的指标回归对比

用法:
  python tools/golden_check.py --json > run_a.json     # 改动前跑一次
  python tools/golden_check.py --json > run_b.json     # 改动后跑一次
  python tools/compare_runs.py run_a.json run_b.json   # 对比

输出 per-site 表格：saved / recall / f1 / section_recall / keyword / 耗时；
状态判定：IMPROVED / SAME / REGRESSION（任一主指标变差即 REGRESSION，
任一变好且无变差为 IMPROVED）；存在回归时退出码 1（CI 可挂钩）。

指标来源与 golden_check 一致：P/R/F1 为保存覆盖率（compute_prf），
section_recall 为期望栏目发现率，keyword_hit 为落盘关键词断言。
"""

import json
import sys
from typing import Any, Optional

# 判定回归的主指标（数值越大越好）
_PRIMARY: tuple = ("saved", "recall", "f1", "section_recall", "keyword_hit")


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _rows(report: dict) -> dict:
    """归一化为 {site_name: metrics}：兼容 sites=列表 与 sites=字典 两种报告。"""
    sites = report.get("sites", report.get("results", {}))
    if isinstance(sites, list):
        return {r.get("name"): r for r in sites}
    return sites


def _metric_status(a: Optional[Any], b: Optional[Any]):
    """比较两个同名单指标。None（缺失）不参与回归判定，只在表格里显示 —。"""
    if a is None and b is None:
        return "SAME", False
    if a is None or b is None:
        return "CHANGED", True
    if a == b:
        return "SAME", False
    return ("IMPROVED" if b > a else "REGRESSION"), True


def _fmt(v: Optional[Any], digits: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def main() -> int:
    if len(sys.argv) != 3:
        print("用法: python tools/compare_runs.py run_a.json run_b.json")
        return 2
    a, b = _load(sys.argv[1]), _load(sys.argv[2])
    ra, rb = _rows(a), _rows(b)
    sites = sorted(set(ra) | set(rb))

    header = f"{'站点':<14}{'saved':<12}{'recall':<12}{'f1':<10}{'section':<10}{'keyword':<10}{'耗时s':<10}状态"
    print(header)
    print("-" * len(header))

    regression = False
    improved = False
    for site in sites:
        sa, sb = ra.get(site, {}), rb.get(site, {})
        ma, mb = sa.get("metrics") or {}, sb.get("metrics") or {}

        cells = []
        statuses = []
        # saved / keyword_hit 取站点顶层字段，其余指标取 metrics 子字段
        for key in _PRIMARY:
            if key in ("saved", "keyword_hit"):
                va = sa.get(key)
                vb = sb.get(key)
            else:
                va = ma.get(key)
                vb = mb.get(key)
            st, _ = _metric_status(va, vb)
            statuses.append(st)
            cells.append(f"{_fmt(va)}→{_fmt(vb)}")

        ta = sa.get("elapsed_s")
        tb = sb.get("elapsed_s")
        cells.append(f"{_fmt(ta, 1)}→{_fmt(tb, 1)}")

        if "REGRESSION" in statuses:
            status = "REGRESSION"
            regression = True
        elif "IMPROVED" in statuses:
            status = "IMPROVED"
            improved = True
        else:
            status = "SAME"

        print(f"{site:<14}{cells[0]:<12}{cells[1]:<12}{cells[2]:<10}{cells[3]:<10}{cells[4]:<10}{cells[5]:<10}{status}")

    # 汇总：平均 recall / f1 / saved 总量
    print("-" * len(header))
    a_rec = [r.get("metrics", {}).get("recall") for r in ra.values()]
    b_rec = [r.get("metrics", {}).get("recall") for r in rb.values()]
    a_f1 = [r.get("metrics", {}).get("f1") for r in ra.values()]
    b_f1 = [r.get("metrics", {}).get("f1") for r in rb.values()]
    a_saved = sum(r.get("saved", 0) for r in ra.values())
    b_saved = sum(r.get("saved", 0) for r in rb.values())
    a_rec_avg = sum(v for v in a_rec if v is not None) / len(a_rec) if a_rec else 0.0
    b_rec_avg = sum(v for v in b_rec if v is not None) / len(b_rec) if b_rec else 0.0
    a_f1_avg = sum(v for v in a_f1 if v is not None) / len(a_f1) if a_f1 else 0.0
    b_f1_avg = sum(v for v in b_f1 if v is not None) / len(b_f1) if b_f1 else 0.0
    print(f"总计 saved: {a_saved} → {b_saved}  |  平均 recall: {a_rec_avg:.3f} → {b_rec_avg:.3f}"
          f"  |  平均 f1: {a_f1_avg:.3f} → {b_f1_avg:.3f}")

    if regression:
        print("\n检测到回归，退出码 1")
        return 1
    if improved:
        print("\n存在改进")
        return 0
    print("\n无回归")
    return 0


if __name__ == "__main__":
    sys.exit(main())
