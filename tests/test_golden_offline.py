"""tests/test_golden_offline.py — golden_check 离线复核模式测试

离线模式（--offline）不联网爬取，只对已落盘的 HTML 做 golden 断言，
让无网络 CI / 本地快速回归也能验证清洗与规则链路没有回归。
此处用 monkeypatch 把 LOCAL_BACKUP_DIR 指向临时目录构造落盘产物。
"""

import os

from tools import golden_check

SITE = {
    "name": "hnbn666",
    "url": "http://www.hnbn666.cn/001_about.html",
    "site_type": "portal",
    "min_saved": 6,
    "keyword": "豫花",
    "desc": "fixture 站点",
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


def test_offline_fails_when_saved_below_min(tmp_path, monkeypatch):
    monkeypatch.setattr(golden_check, "LOCAL_BACKUP_DIR", str(tmp_path))
    _write_html(str(tmp_path), "www.hnbn666.cn/s1.html")  # 只落 1 个
    r = golden_check._offline_one(SITE)
    assert r["ok"] is False
    assert any("saved=1 < 期望 6" in reason for reason in r["reasons"])


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
