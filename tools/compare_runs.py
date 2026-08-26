"""tools/compare_runs.py — golden 两次运行的指标回归对比

用法:
  python tools/golden_check.py --json > run_a.json     # 改动前跑一次
  python tools/golden_check.py --json > run_b.json     # 改动后跑一次
  python tools/compare_runs.py run_a.json run_b.json   # 对比

输出 per-site 表格：saved 数量、关键词命中、耗时；
状态判定：IMPROVED / SAME / REGRESSION；存在回归时退出码 1（CI 可挂钩）。
"""

import json
import os
import sys


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _rows(report: dict) -> dict:
    """归一化为 {site_name: metrics}：兼容 sites=列表 与 sites=字典 两种报告。"""
    sites = report.get("sites", report.get("results", {}))
    if isinstance(sites, list):
        return {r.get("name"): r for r in sites}
    return sites


def main() -> int:
    if len(sys.argv) != 3:
        print("用法: python tools/compare_runs.py run_a.json run_b.json")
        return 2
    a, b = _load(sys.argv[1]), _load(sys.argv[2])
    ra, rb = _rows(a), _rows(b)
    sites = sorted(set(ra) | set(rb))

    header = f"{'站点':<24}{'saved(A→B)':<14}{'keyword(A→B)':<16}{'耗时s(A→B)':<14}状态"
    print(header)
    print("-" * len(header))

    regression = False
    for site in sites:
        sa = ra.get(site, {}).get("saved")
        sb = rb.get(site, {}).get("saved")
        ka = ra.get(site, {}).get("keyword_hit")
        kb = rb.get(site, {}).get("keyword_hit")
        ta = ra.get(site, {}).get("elapsed_s")
        tb = rb.get(site, {}).get("elapsed_s")

        saved_d = "—" if (sa is None and sb is None) else f"{sa if sa is not None else '—'}→{sb if sb is not None else '—'}"
        kw_d = "—" if (ka is None and kb is None) else f"{'True' if ka else 'False'}→{'True' if kb else 'False'}"
        t_d = "—" if (ta is None and tb is None) else f"{ta if ta is not None else '—'}→{tb if tb is not None else '—'}"

        status = "SAME"
        if sa is not None and sb is not None and sa != sb:
            status = "IMPROVED" if sb > sa else "REGRESSION"
            if sb < sa:
                regression = True
        if ka is not None and kb is not None and bool(ka) != bool(kb):
            status = "IMPROVED" if kb else "REGRESSION"
            if ka and not kb:
                regression = True

        print(f"{site:<24}{saved_d:<14}{kw_d:<16}{t_d:<14}{status}")

    # 汇总
    a_saved = sum(r.get("saved", 0) for r in ra.values())
    b_saved = sum(r.get("saved", 0) for r in rb.values())
    a_hit = sum(1 for r in ra.values() if r.get("keyword_hit"))
    b_hit = sum(1 for r in rb.values() if r.get("keyword_hit"))
    print("-" * len(header))
    print(f"总计 saved: {a_saved} → {b_saved}  |  关键词命中站点: {a_hit} → {b_hit}")

    if regression:
        print("\n检测到回归，退出码 1")
        return 1
    print("\n无回归")
    return 0


if __name__ == "__main__":
    sys.exit(main())
