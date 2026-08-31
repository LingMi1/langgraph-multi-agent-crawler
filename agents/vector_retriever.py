"""agents/vector_retriever.py — 轻量向量检索（RAG 检索端，纯 Python 零依赖）

用字符 n-gram **TF-IDF 稀疏向量 + 余弦相似度**做语义检索，把 RAG 的
"切分 → 向量化 → 索引 → 检索 → 排序"链路跑通：

  - 不依赖 numpy/sklearn/embedding 模型：离线可跑、单元可测、原理可讲
  - 字符 n-gram 天然适配中英混排，无需分词器
  - 配合 `agents/react.py`（Function Calling）可让 Agent 通过 `rag_search`
    工具查询爬取页面构成的知识库——"爬虫 → 建库 → Agent 问答"闭环

设计取舍：
  - TF-IDF 是经典统计检索基线（面试叙事：理解 BM25 的上位替代关系）
  - 稀疏向量用 dict 表示，余弦=点积/(模长积)，大数据量再换 numpy/向量库
  - 重排序（search_reranked）用两阶段范式：TF-IDF 初筛 → 伪相关反馈(PRF)
    查询扩展 + 得分融合，零依赖、可解释、单元可测（对应 RAG 管线的
    "retrieve → rerank" 阶段）
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agents.semdedup import ngrams


class VectorIndex:
    """增量建索引：add 收集文档，build 计算 IDF，search 做余弦检索。

    - add(text)     返回文档 id（从 0 递增）
    - build()       全部 add 后调用一次，计算全局 IDF
    - search(q,k)   返回 [(doc_id, score, snippet), ...] 按相似度降序
    """

    def __init__(self, n: int = 3, min_df: int = 1):
        self.n = n
        self.min_df = min_df
        self._docs: List[str] = []
        self._tf: List[Counter] = []          # 每篇文档的 n-gram 计数
        self._df: Counter = Counter()          # 每个 n-gram 出现在几篇文档
        self._idf: Dict[str, float] = {}
        self._built = False

    # ── 建索引 ──

    def add(self, text: str) -> int:
        doc_id = len(self._docs)
        gram_set = ngrams(text, self.n)
        tf = Counter(gram_set)
        self._docs.append(text)
        self._tf.append(tf)
        for g in tf:
            self._df[g] += 1
        self._built = False
        return doc_id

    def add_many(self, texts: Sequence[str]) -> List[int]:
        return [self.add(t) for t in texts]

    def build(self) -> None:
        """计算 IDF（平滑版，避免分母为 0）。"""
        n_docs = len(self._docs)
        self._idf = {
            g: math.log((n_docs + 1) / (df + 1)) + 1.0
            for g, df in self._df.items()
            if df >= self.min_df
        }
        self._built = True

    # ── 向量化 ──

    def _vector(self, text: str) -> Dict[str, float]:
        """TF-IDF 稀疏向量（dict: gram -> weight）。"""
        if not self._built:
            self.build()
        tf = Counter(ngrams(text, self.n))
        vec: Dict[str, float] = {}
        for g, c in tf.items():
            w = self._idf.get(g, 0.0)
            if w > 0:
                vec[g] = c * w
        return vec

    @staticmethod
    def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        dot = 0.0
        # 遍历较小的一方
        small, big = (a, b) if len(a) <= len(b) else (b, a)
        for g, w in small.items():
            wb = big.get(g)
            if wb:
                dot += w * wb
        norm_a = math.sqrt(sum(w * w for w in a.values())) or 1.0
        norm_b = math.sqrt(sum(w * w for w in b.values())) or 1.0
        return dot / (norm_a * norm_b)

    # ── 检索 ──

    def search(self, query: str, top_k: int = 5) -> List[Tuple[int, float, str]]:
        """语义检索：返回 [(doc_id, score, snippet)]，snippet 为正文前 60 字。"""
        qv = self._vector(query)
        scored = [(i, self._cosine(qv, self._vector(d))) for i, d in enumerate(self._docs)]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [
            (i, round(score, 4), self._docs[i].strip()[:60])
            for i, score in scored[:top_k]
            if score > 0.0
        ]

    def search_reranked(
        self,
        query: str,
        top_k: int = 5,
        alpha: float = 0.6,
        fb_docs: int = 1,
        fb_grams: int = 8,
    ) -> List[Tuple[int, float, str]]:
        """两阶段重排序：TF-IDF 初筛 → 伪相关反馈(PRF)查询扩展 → 得分融合。

        - 阶段 1：余弦初筛，取 score > 0 的候选
        - 阶段 2：把候选榜首（前 fb_docs 篇）中权重最高的 fb_grams 个 n-gram
          按 0.5 权重并入查询向量（Rocchio 式伪相关反馈，零依赖）；
          最终得分 = alpha * 初筛分 + (1 - alpha) * 扩展后分（融合，抗单信号噪声）

        alpha 越小越依赖扩展信号；fb_docs/fb_grams 为 0 时退化为普通 search。
        """
        qv = self._vector(query)
        base = [(i, self._cosine(qv, self._vector(d))) for i, d in enumerate(self._docs)]
        base = [(i, s) for i, s in base if s > 0.0]
        base.sort(key=lambda x: x[1], reverse=True)
        if not base:
            return []
        # PRF：从高相关文档摘取区分性 n-gram 扩充查询
        expanded = dict(qv)
        for i, _ in base[:fb_docs]:
            dv = self._vector(self._docs[i])
            top = sorted(dv.items(), key=lambda kv: kv[1], reverse=True)[:fb_grams]
            for g, w in top:
                expanded[g] = expanded.get(g, 0.0) + 0.5 * w
        fused = [
            (i, alpha * s0 + (1.0 - alpha) * self._cosine(expanded, self._vector(self._docs[i])))
            for i, s0 in base
        ]
        fused.sort(key=lambda x: x[1], reverse=True)
        return [
            (i, round(s, 4), self._docs[i].strip()[:60])
            for i, s in fused[:top_k]
        ]

    def __len__(self) -> int:
        return len(self._docs)


def build_rag_tool(index: VectorIndex, top_k: int = 5, rerank: bool = False) -> Any:
    """把一个已建好的索引包装成 Tool（绑定闭包执行器）。

    用法：注册进 ToolRegistry 后，Agent 可通过 Function Calling 查询知识库。
    rerank=True 时走两阶段重排序（PRF 查询扩展 + 得分融合）。
    """
    from agents.tools import Tool

    def _search(query: str, k: Optional[int] = None):
        k = k or top_k
        if rerank:
            return index.search_reranked(query, top_k=k)
        return index.search(query, top_k=k)

    return Tool(
        "rag_search", "在爬取页面知识库中做语义检索，返回 top-k 相关片段",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": top_k},
            },
            "required": ["query"],
        },
        _search,
    )
