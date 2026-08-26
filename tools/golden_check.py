"""tools/golden_check.py — 离线评估集（Golden Set）回归检查

对一组"已知答案"的站点跑完整爬取，断言关键指标（保存量 / 落盘关键词），
用于验证清洗链路与规则改动没有引入回归。

用法:
  python tools/golden_check.py            # 跑全部 golden 站点
  python tools/golden_check.py hnbn666   # 只跑匹配的站点
  python tools/golden_check.py --list    # 列出 golden 清单
"""

import argparse
import asyncio
import os
import re
import sys
from urllib.parse import urlparse

# 保证从项目根目录可导入（config / graph / agents）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    # 新增站点时按此模板扩展：
    # {
    #     "name": "example",
    #     "url": "https://example.com/",
    #     "site_type": "cms",
    #     "min_saved": 3,
    #     "keyword": "公司",
    #     "desc": "示例站点描述",
    # },
]


def _run_one(site: dict, max_steps: int = 3000) -> dict:
    """跑单个 golden 站点，返回结构化评估结果（含指标与耗时）。"""
    import time

    t0 = time.time()
    stats = {"saved": 0, "failed": 0}
    try:
        stats = asyncio.run(run_crawler(site["url"], max_steps=max_steps))
    except Exception as e:  # noqa: BLE001 —— golden 检查要吞异常并给出失败原因
        return {
            "name": site["name"], "ok": False, "saved": 0, "min_saved": site["min_saved"],
            "keyword_hit": False, "reasons": [f"爬取异常: {e}"],
            "elapsed_s": round(time.time() - t0, 1),
        }

    ok = True
    reasons = []

    saved = stats.get("saved", 0)
    if saved < site["min_saved"]:
        ok = False
        reasons.append(f"saved={saved} < 期望 {site['min_saved']}")

    keyword_hit = True
    if site.get("keyword"):
        netloc = urlparse(site["url"]).netloc.replace(":", "_")
        out_dir = os.path.join(LOCAL_BACKUP_DIR, netloc)
        keyword_hit = False
        # 落盘 HTML 按栏目子目录存放 → 递归遍历
        for root, _, fs in os.walk(out_dir):
            for name in fs:
                if not name.endswith(".html"):
                    continue
                try:
                    with open(os.path.join(root, name), encoding="utf-8", errors="ignore") as f:
                        if site["keyword"] in f.read():
                            keyword_hit = True
                            break
                except OSError:
                    continue
            if keyword_hit:
                break
        if not keyword_hit:
            ok = False
            reasons.append(f"落盘内容未找到关键词 {site['keyword']!r}")

    return {
        "name": site["name"], "ok": ok, "saved": saved, "min_saved": site["min_saved"],
        "keyword_hit": keyword_hit, "reasons": reasons,
        "elapsed_s": round(time.time() - t0, 1),
    }


def _format_result(r: dict) -> str:
    status = "PASS" if r["ok"] else "FAIL"
    detail = f"saved={r['saved']}"
    if r.get("reasons"):
        detail += " | " + "; ".join(r["reasons"])
    return f"[{status}] {r['name']:<10} {detail} ({r['elapsed_s']}s)"


def main() -> int:
    parser = argparse.ArgumentParser(description="Golden Set 离线评估")
    parser.add_argument("filter", nargs="?", default="", help="站点名过滤（正则）")
    parser.add_argument("--list", action="store_true", help="仅列出 golden 清单")
    parser.add_argument("--json", action="store_true",
                        help="输出结构化 JSON 报告（CI / 指标采集用）")
    args = parser.parse_args()

    if args.list:
        for s in GOLDEN_SITES:
            print(f"  {s['name']:<10} {s['desc']}")
        return 0

    sites = [s for s in GOLDEN_SITES if re.search(args.filter, s["name"], re.I)]
    if not sites:
        print(f"未匹配到站点: {args.filter!r}")
        return 2

    results = [_run_one(s) for s in sites]
    failed = sum(0 if r["ok"] else 1 for r in results)

    if args.json:
        import json
        report = {
            "suite": "golden_set",
            "total": len(results),
            "passed": len(results) - failed,
            "failed": failed,
            "sites": results,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(_format_result(r))
        print("-" * 60)
        print(f"Golden 评估完成 | 通过={len(results) - failed} 失败={failed} / 共 {len(results)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
