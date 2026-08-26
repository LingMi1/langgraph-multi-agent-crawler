"""tests/test_semdedup.py — 轻量 RAG 语义去重（n-gram Jaccard）测试

覆盖：ngrams 提取、jaccard_similarity 边界、near_duplicate_pages 批量检测、
ToolRegistry 注册与调用。
"""

import pytest

from agents.semdedup import (
    jaccard_similarity,
    near_duplicate_pages,
    ngrams,
)
from agents.tools import ToolRegistry


# ---------- ngrams ----------

def test_ngrams_basic_cjk():
    g = ngrams("豫花花生油产品展示", 3)
    assert "豫花花" in g
    assert "花生油" in g
    assert len(g) > 0


def test_ngrams_normalizes_whitespace():
    a = ngrams("  河南  花生   油  ", 3)
    b = ngrams("河南 花生 油", 3)
    assert a == b


def test_ngrams_short_text():
    assert ngrams("ab", 3) == {"ab"}
    assert ngrams("", 3) == set()


# ---------- jaccard_similarity ----------

def test_jaccard_identical():
    assert jaccard_similarity("河南豫花实业有限公司", "河南豫花实业有限公司") == pytest.approx(1.0)


def test_jaccard_near_duplicate():
    a = "豫花花生油，精选优质花生，传统工艺压榨，口感香醇。"
    b = "豫花花生油，精选优质花生，传统工艺压榨，口感醇香。"  # 少量差异
    assert jaccard_similarity(a, b) > 0.7


def test_jaccard_different_pages():
    a = "豫花花生油产品介绍与公司新闻"
    b = "招聘信息：诚聘销售经理，要求本科以上学历"
    assert jaccard_similarity(a, b) < 0.3


def test_jaccard_empty_inputs():
    assert jaccard_similarity("", "abc") == 0.0
    assert jaccard_similarity("", "") == 0.0


def test_jaccard_same_with_small_edit():
    a = "产品展示" * 20
    b = "产品展示" * 19 + "新品上市"
    assert jaccard_similarity(a, b) >= 0.5


# ---------- near_duplicate_pages ----------

def test_batch_finds_duplicates():
    base = "豫花花生油产品详情介绍，来自河南豫花实业有限公司。" * 5
    texts = [
        base,
        base[:-2] + "。" * 2,       # 近重复
        "公司招聘信息页面，与产品内容完全无关。" * 3,
    ]
    pairs = near_duplicate_pages(texts, threshold=0.6)
    assert (0, 1) in [p[:2] for p in pairs]


def test_batch_keeps_distinct_pages():
    texts = [
        "产品A：花生油压榨工艺介绍。",
        "产品B：芝麻油传统工艺介绍。",
        "新闻C：公司年会活动报道。",
    ]
    pairs = near_duplicate_pages(texts, threshold=0.7)
    assert pairs == []


def test_batch_empty_and_single():
    assert near_duplicate_pages([]) == []
    assert near_duplicate_pages(["只有一页"]) == []


# ---------- ToolRegistry 注册 ----------

def test_semdedup_tools_registered():
    reg = ToolRegistry.builtin()
    assert "jaccard_similarity" in reg.names()
    assert "near_duplicate_pages" in reg.names()


def test_semdedup_tool_callable():
    reg = ToolRegistry.builtin()
    score = reg.call("jaccard_similarity", a="同一段文本内容", b="同一段文本内容")
    assert score == pytest.approx(1.0)
    pairs = reg.call("near_duplicate_pages", texts=["aaaa", "aaaa", "bbbb"], threshold=0.6)
    assert (0, 1) in [p[:2] for p in pairs]


def test_semdedup_tool_schema_valid():
    reg = ToolRegistry.builtin()
    schema = reg.get("jaccard_similarity").schema()
    assert schema["type"] == "function"
    assert "a" in schema["function"]["parameters"]["properties"]
    assert schema["function"]["parameters"]["required"] == ["a", "b"]
