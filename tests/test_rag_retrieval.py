"""tests/test_rag_retrieval.py — VectorIndex（TF-IDF 字符 n-gram 余弦检索）单元测试。

验证 RAG 检索链路：建索引 / 语义命中 / 排序 / 边界；以及 rag_search 工具形态。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agents.semdedup import ngrams
from agents.tools import ToolRegistry
from agents.vector_retriever import VectorIndex, build_rag_tool

_DOCS = [
    "公司成立于2005年，主营业务为花生油、菜籽油的压榨生产与销售，产品覆盖华东华北市场。",
    "公司招聘岗位包括设备操作工、质检员与销售代表，联系方式见官网，地址在河南新乡。",
    "花生油采用传统物理压榨工艺，不添加任何防腐剂，出油率稳定在42%以上。",
    "本网站提供花生系列产品的详细介绍，包括规格、包装与检测报告下载。",
]


@pytest.fixture
def idx():
    i = VectorIndex()
    i.add_many(_DOCS)
    i.build()
    return i


class TestNgrams:
    def test_ngram_length(self):
        grams = ngrams("压榨工艺", n=2)
        assert all(len(g) == 2 for g in grams)
        assert "压榨" in grams

    def test_ngram_empty(self):
        assert ngrams("", 3) == set()
        assert ngrams("ab", 3) == {"ab"}  # 短于 n 返回整体（非空时）


class TestVectorIndex:
    def test_len(self, idx):
        assert len(idx) == 4

    def test_semantic_hit(self, idx):
        """语义相近的查询应命中对应文档（含子串重合度低的也能靠 n-gram 命中）。"""
        hits = [d for d, _, _ in idx.search("花生油的压榨工艺", top_k=4)]
        assert hits[0] == 2  # 工艺文档排第一

    def test_company_profile_hit(self, idx):
        hits = [d for d, _, _ in idx.search("公司简介和主营业务", top_k=4)]
        assert hits[0] == 0

    def test_contact_recruit_hit(self, idx):
        hits = [d for d, _, _ in idx.search("招聘联系方式", top_k=4)]
        assert hits[0] == 1

    def test_score_ordering_desc(self, idx):
        res = idx.search("花生油 压榨 工艺 生产", top_k=4)
        scores = [s for _, s, _ in res]
        assert scores == sorted(scores, reverse=True)
        assert all(s > 0 for s in scores)

    def test_query_not_in_index_still_scores(self, idx):
        """完全无关查询返回 0 或很低的相似度（可能为空列表）。"""
        res = idx.search("钢铁冶炼高炉温度", top_k=4)
        assert len(res) == 0 or res[0][1] < 0.05

    def test_snippet_trimmed(self, idx):
        _, _, snip = idx.search("花生油", top_k=1)[0]
        assert len(snip) <= 64

    def test_add_after_build_rebuilds_idf(self):
        i = VectorIndex()
        i.add("文档A 的内容是关于花生油的。")
        i.build()
        assert i.search("花生油", top_k=1)  # 建好后能检索
        i.add("新文档 关于菜籽油的内容。")
        i.build()
        assert len(i) == 2

    def test_empty_index_search(self):
        i = VectorIndex()
        i.build()
        assert i.search("任意查询", top_k=3) == []

    def test_same_text_top_score(self, idx):
        """查询与文档完全一致时相似度最高。"""
        res = idx.search(_DOCS[3], top_k=4)
        assert res[0][0] == 3


class TestRagTool:
    def test_build_rag_tool_shape(self, idx):
        tool = build_rag_tool(idx, top_k=2)
        schema = tool.schema()
        assert schema["function"]["name"] == "rag_search"
        assert schema["type"] == "function"

    def test_registered_and_called(self, idx):
        reg = ToolRegistry()
        reg.register(build_rag_tool(idx))
        res = reg.call("rag_search", query="花生油工艺", k=2)
        assert len(res) == 2
        assert res[0][0] == 2
