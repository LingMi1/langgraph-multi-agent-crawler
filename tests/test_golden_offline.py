"""tests/test_golden_offline.py — golden_check 离线复核模式测试

离线模式（--offline）不联网爬取，只对已落盘的 HTML 做 golden 断言，
让无网络 CI / 本地快速回归也能验证清洗与规则链路没有回归。
此处用 monkeypatch 把 LOCAL_BACKUP_DIR 指向临时目录构造落盘产物。
"""

import os

import pytest

from tools import golden_check

SITE = {
    "name": "hnbn666",
    "url": "http://www.hnbn666.cn/001_about.html",
    "site_type": "portal",
    "min_saved": 6,
    "keyword": "豫花",
    "desc": "fixture 站点",
}

SITE_WITH_SECTIONS = {
    **SITE,
    "expected_saved": 8,
    "expected_sections": ["关于", "产品", "新闻"],
}


def _write_html(out_dir: str, rel: str, keyword: bool = True) -> None:
    path = os.path.join(out_dir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("<html><body>豫花产品</body></html>" if keyword else "<html><body>无关键词</body></html>")


def test_offline_pass_when_artifacts_meet_golden(tmp_path, monkeypatch):
    monkeypatch.setattr(golden_check, "LOCAL_BACKUP_DIR", str(tmp_path))
    for i in range(6):  # 达到 min_saved 且含关键词
        _write_html(str(tmp_path), f"www.hnbn666.cn/s{i}.html")
    r = golden_check._offline_one(SITE)
    assert r["ok"] is True and r["saved"] == 6 and r["keyword_hit"] is True
    assert r["offline"] is True
    # 指标：saved=expected=6 → 全量命中，P/R/F1 均 1.0
    assert r["metrics"]["recall"] == 1.0 and r["metrics"]["precision"] == 1.0
    assert r["metrics"]["f1"] == 1.0 and r["metrics"]["coverage"] == 1.0


def test_offline_fails_when_saved_below_min(tmp_path, monkeypatch):
    monkeypatch.setattr(golden_check, "LOCAL_BACKUP_DIR", str(tmp_path))
    _write_html(str(tmp_path), "www.hnbn666.cn/s1.html")  # 只落 1 个
    r = golden_check._offline_one(SITE)
    assert r["ok"] is False
    assert any("saved=1 < 期望 6" in reason for reason in r["reasons"])
    # 指标：overlap=min(1,6)=1 → recall=1/6
    assert r["metrics"]["recall"] == pytest.approx(1 / 6, abs=1e-3)
    assert r["metrics"]["f1"] == pytest.approx(2 / 7, abs=1e-3)


def test_offline_fails_when_keyword_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(golden_check, "LOCAL_BACKUP_DIR", str(tmp_path))
    for i in range(6):
        _write_html(str(tmp_path), f"www.hnbn666.cn/s{i}.html", keyword=False)
    r = golden_check._offline_one(SITE)
    assert r["ok"] is False and r["keyword_hit"] is False
    assert any("关键词" in reason for reason in r["reasons"])


def test_offline_counts_nested_column_dirs(tmp_path, monkeypatch):
    # 落盘 HTML 按栏目子目录存放 → 递归计数必须生效
    monkeypatch.setattr(golden_check, "LOCAL_BACKUP_DIR", str(tmp_path))
    for i in range(6):
        _write_html(str(tmp_path), f"www.hnbn666.cn/col{i % 2}/page{i}.html")
    r = golden_check._offline_one(SITE)
    assert r["saved"] == 6 and r["ok"] is True


def test_offline_section_recall(tmp_path, monkeypatch):
    # 期望栏目 3 个，落盘目录只发现 2 个 → section_recall = 2/3
    monkeypatch.setattr(golden_check, "LOCAL_BACKUP_DIR", str(tmp_path))
    _write_html(str(tmp_path), "www.hnbn666.cn/关于/page.html")
    _write_html(str(tmp_path), "www.hnbn666.cn/产品/page.html")
    _write_html(str(tmp_path), "www.hnbn666.cn/产品/page2.html")
    r = golden_check._offline_one(SITE_WITH_SECTIONS)
    assert r["metrics"]["section_recall"] == pytest.approx(2 / 3, abs=1e-3)
    assert r["metrics"]["recall"] == pytest.approx(3 / 8, abs=1e-3)  # expected_saved=8，实际只落 3
    assert r["ok"] is False  # saved=3 < min_saved=6


# ── 任务成功率：预算约束（区分"完成质量"与"资源效率"） ──

def test_offline_success_equals_ok_and_budget_always_ok(tmp_path, monkeypatch):
    # 离线无 LLM 成本：budget_ok 恒真，success 与 ok 一致（兼容旧口径）
    monkeypatch.setattr(golden_check, "LOCAL_BACKUP_DIR", str(tmp_path))
    for i in range(6):
        _write_html(str(tmp_path), f"www.hnbn666.cn/s{i}.html")
    r = golden_check._offline_one(SITE)
    assert r["success"] is True and r["budget_ok"] is True and r["budget"] == {}
    os.remove(os.path.join(str(tmp_path), "www.hnbn666.cn/s1.html"))  # 删 1 个 → saved=5 < 6
    r2 = golden_check._offline_one(SITE)
    assert r2["success"] is False and r2["ok"] is False


def test_budget_delta_only_counts_this_task():
    # 快照差分：只统计本次任务的增量，不跨站点累计
    before = {"total": {"calls": 10, "prompt_tokens": 1000, "completion_tokens": 200, "cost": 0.05}}
    after = {"total": {"calls": 15, "prompt_tokens": 1600, "completion_tokens": 300, "cost": 0.08}}
    d = golden_check._budget_delta(after, before)
    assert d["total"]["calls"] == 5
    assert d["total"]["prompt_tokens"] == 600
    assert d["total"]["completion_tokens"] == 100
    assert d["total"]["cost"] == pytest.approx(0.03)
    assert golden_check._budget_delta({}, before) == {}


def test_budget_within_cap_pass_and_fail():
    cap = {"max_calls": 10, "max_tokens": 5000, "max_cost": 0.5}
    ok_budget = {"total": {"calls": 8, "prompt_tokens": 3000, "completion_tokens": 500, "cost": 0.2}}
    within, reasons = golden_check._budget_within_cap(ok_budget, cap)
    assert within is True and reasons == []

    over_calls = {"total": {"calls": 11, "prompt_tokens": 3000, "completion_tokens": 500, "cost": 0.2}}
    within, reasons = golden_check._budget_within_cap(over_calls, cap)
    assert within is False
    assert any("calls=11" in r for r in reasons)

    over_cost = {"total": {"calls": 3, "prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.9}}
    within, reasons = golden_check._budget_within_cap(over_cost, cap)
    assert within is False
    assert any("cost" in r for r in reasons)


def test_budget_within_cap_no_data_not_scored():
    # 未配置 LLM / 无预算数据 → 不评分（不因缺数据误判任务失败）
    within, reasons = golden_check._budget_within_cap({}, None)
    assert within is True and reasons == []
