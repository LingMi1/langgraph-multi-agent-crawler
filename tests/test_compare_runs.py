"""tests/test_compare_runs.py — compare_runs 指标回归判定测试

验证：全指标 diff 中任一主指标变差 → REGRESSION（返回 1）；
任一变好且无变差 → IMPROVED；全部相同 → SAME。
"""

import json
import sys

from tools import compare_runs


def _report(site_results: list) -> dict:
    return {"suite": "golden_set", "total": len(site_results),
            "passed": 1, "failed": 0, "sites": site_results}


def _site(name: str, saved: int, recall: float, f1: float, keyword: bool) -> dict:
    return {
        "name": name, "ok": True, "saved": saved, "min_saved": saved,
        "keyword_hit": keyword,
        "metrics": {"precision": round(recall, 4), "recall": recall, "f1": f1,
                    "coverage": recall, "section_recall": None},
        "reasons": [], "elapsed_s": 1.0,
    }


def _write(tmp_path, name: str, report: dict) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _run(tmp_path, monkeypatch, a, b):
    monkeypatch.setattr(sys, "argv", ["compare_runs", _write(tmp_path, "a.json", a),
                                      _write(tmp_path, "b.json", b)])
    return compare_runs.main()


def test_compare_detects_regression(tmp_path, monkeypatch, capsys):
    a = _report([_site("s1", saved=6, recall=1.0, f1=1.0, keyword=True)])
    b = _report([_site("s1", saved=6, recall=0.5, f1=0.667, keyword=True)])
    assert _run(tmp_path, monkeypatch, a, b) == 1
    assert "REGRESSION" in capsys.readouterr().out


def test_compare_detects_improvement(tmp_path, monkeypatch, capsys):
    a = _report([_site("s1", saved=4, recall=0.5, f1=0.667, keyword=True)])
    b = _report([_site("s1", saved=6, recall=1.0, f1=1.0, keyword=True)])
    assert _run(tmp_path, monkeypatch, a, b) == 0
    assert "IMPROVED" in capsys.readouterr().out


def test_compare_same_no_change(tmp_path, monkeypatch, capsys):
    a = _report([_site("s1", saved=6, recall=1.0, f1=1.0, keyword=True)])
    b = _report([_site("s1", saved=6, recall=1.0, f1=1.0, keyword=True)])
    assert _run(tmp_path, monkeypatch, a, b) == 0
    assert "无回归" in capsys.readouterr().out


def test_compare_keyword_regression_triggers_exit1(tmp_path, monkeypatch):
    a = _report([_site("s1", saved=6, recall=1.0, f1=1.0, keyword=True)])
    b = _report([_site("s1", saved=6, recall=1.0, f1=1.0, keyword=False)])
    assert _run(tmp_path, monkeypatch, a, b) == 1
