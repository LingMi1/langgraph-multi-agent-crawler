"""agents/semdedup.py — 轻量 RAG 语义去重（n-gram Jaccard，无外部依赖）

大厂叙事：近重复页面（同一栏目模板 + 少量差异）无法用 MD5 精确去重命中，
用字符级 n-gram Jaccard 相似度做"软去重"——低成本、确定性、可解释。

纯 Python 实现（字符 n-gram 天然适配中英混排），不引入 jieba/embedding，
适合作为去重管道的第二道闸（第一道是 _url_key + md5 精确去重）。
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple


def ngrams(text: str, n: int = 3) -> set:
    """提取字符级 n-gram 集合。

    - CJK 按单字切分即可表达语义片段（无需分词器）
    - 空白归一化后生成，避免缩进差异干扰
    """
    norm = " ".join(text.split())
    if len(norm) < n:
        return {norm} if norm else set()
    return {norm[i:i + n] for i in range(len(norm) - n + 1)}


def jaccard_similarity(a: str, b: str, n: int = 3) -> float:
    """两个文本的 n-gram Jaccard 相似度（0.0 ~ 1.0）。"""
    ga, gb = ngrams(a, n), ngrams(b, n)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    union = len(ga | gb)
    return inter / union if union else 0.0


def near_duplicate_pages(texts: Sequence[str], threshold: float = 0.6,
                         n: int = 3) -> List[Tuple[int, int, float]]:
    """批量近重复检测：返回 [(i, j, score), ...]（i<j，score>=threshold）。

    用途：把同一栏目下"标题不同但正文几乎相同"的重复页标记出来，
    供去重管道决定只保留第一条（RAG 场景下避免冗余入向量库）。
    """
    results: List[Tuple[int, int, float]] = []
    # 先做一次 n-gram 预计算，避免 O(n^2) 内重复切分
    gram_sets: Dict[int, set] = {i: ngrams(t, n) for i, t in enumerate(texts)}
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            gi, gj = gram_sets[i], gram_sets[j]
            if not gi or not gj:
                continue
            inter = len(gi & gj)
            union = len(gi | gj)
            score = inter / union if union else 0.0
            if score >= threshold:
                results.append((i, j, round(score, 4)))
    return results
