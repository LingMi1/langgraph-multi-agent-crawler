"""tests/test_prompt_registry.py — 提示词版本管理：sha256 指纹 + 漂移检测。"""

from tools import prompt_registry


def test_sha256_deterministic(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("你好 prompt", encoding="utf-8")
    assert prompt_registry.sha256_file(str(p)) == prompt_registry.sha256_file(str(p))


def test_build_manifest_counts(tmp_path):
    (tmp_path / "提取提示词.txt").write_text("l1\nl2", encoding="utf-8")
    m = prompt_registry.build_manifest(str(tmp_path))
    entry = m["提取提示词.txt"]
    assert entry["bytes"] == len("l1\nl2".encode("utf-8"))
    assert entry["lines"] == 2
    # 未提供的文件 → 指纹 None（缺失可检测）
    assert m["正文渲染代码.txt"]["sha256"] is None


def test_drift_detected_when_prompt_changes(tmp_path):
    p = tmp_path / "清洗提示词.txt"
    p.write_text("v1", encoding="utf-8")
    baseline = prompt_registry.build_manifest(str(tmp_path))
    p.write_text("v2", encoding="utf-8")
    ok, diffs = prompt_registry.check_drift(str(tmp_path), baseline)
    assert ok is False and any("清洗提示词" in d for d in diffs)


def test_no_drift_when_unchanged(tmp_path):
    (tmp_path / "清洗提示词.txt").write_text("v1", encoding="utf-8")
    baseline = prompt_registry.build_manifest(str(tmp_path))
    ok, diffs = prompt_registry.check_drift(str(tmp_path), baseline)
    assert ok is True and diffs == []


def test_missing_baseline_flagged(tmp_path):
    ok, diffs = prompt_registry.check_drift(str(tmp_path), None)
    assert ok is False and any("基线" in d for d in diffs)
