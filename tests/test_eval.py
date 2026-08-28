"""tests/test_eval.py — Agent 评估体系：P/R/F1 + 确定性打分 + 检索指标 + LLM-as-judge。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.eval import (
    compute_prf,
    heuristic_score,
    judge_agreement,
    llm_judge,
    ndcg_at_k,
    parse_judge_json,
    recall_at_k,
)


class TestHeuristicScore:
    def test_rich_page(self):
        text = "<p>" + "花生油压榨工艺介绍。" * 400 + "</p><a href='/a'>x</a><img src='i1'><img src='i2'>"
        r = heuristic_score(text)
        assert r["score"] == 0.9  # 0.5 正文充足 + 0.2 链接正常 + 0.2 图片完整

    def test_empty(self):
        r = heuristic_score("")
        assert r["score"] == 0.2  # 0.1 正文过短 + 0.1 无链接 + 0 图片
        assert r["text_len"] == 0

    def test_short_no_image(self):
        r = heuristic_score("短文" * 200)  # 400 字符 → 正文一般
        assert r["score"] == 0.4  # 0.3 正文一般 + 0.1 无链接 + 0 图片
        assert "正文一般" in r["reasons"]

    def test_too_many_links(self):
        text = "正文" * 800 + "".join(f"<a href='/{i}'>{i}</a>" for i in range(30))
        r = heuristic_score(text)
        # 长度1600→0.5 正文充足; 链接30→链接过多(0分); 图片0
        assert r["score"] == 0.5
        assert "链接过多" in r["reasons"]


class TestRetrievalMetrics:
    def test_recall_at_k_perfect(self):
        assert recall_at_k([0, 1, 2, 3], {0, 1}, 2) == 1.0

    def test_recall_at_k_partial(self):
        assert recall_at_k([0, 5, 1, 2], {0, 1}, 2) == 0.5  # k=2 只命中 0

    def test_recall_at_k_beyond_list(self):
        assert recall_at_k([0], {0, 1}, 5) == 0.5

    def test_recall_no_relevant(self):
        assert recall_at_k([0, 1], set(), 2) == 0.0

    def test_ndcg_perfect(self):
        # 理想位置：相关文档都排最前 → NDCG=1
        assert ndcg_at_k([0, 1], {0, 1}, 2) == 1.0

    def test_ndcg_position_penalty(self):
        # 相关文档排在次位：DCG=1/log2(3)=0.6309, IDCG=1 → NDCG=0.6309
        assert ndcg_at_k([5, 0], {0}, 2) == 0.6309

    def test_ndcg_ideal(self):
        import math
        assert ndcg_at_k([0, 1, 2], {2}, 3) == round(
            1 / math.log2(4), 4)  # 相关文档在第 3 位


class TestPrf:
    def test_perfect(self):
        m = compute_prf(expected=5, actual=5, overlap=5)
        assert m == {"precision": 1.0, "recall": 1.0, "f1": 1.0}

    def test_no_output(self):
        m = compute_prf(expected=5, actual=0, overlap=0)
        assert m["precision"] == 0.0 and m["recall"] == 0.0 and m["f1"] == 0.0

    def test_half_recall(self):
        """产出 10 个命中 5 个（期望 10）：precision=0.5 recall=0.5 f1=0.5。"""
        m = compute_prf(expected=10, actual=10, overlap=5)
        assert m["precision"] == 0.5 and m["recall"] == 0.5 and m["f1"] == 0.5

    def test_precision_recall_tradeoff(self):
        """期望 10、产出 4、命中 3：precision 高 recall 低。"""
        m = compute_prf(expected=10, actual=4, overlap=3)
        assert m["precision"] == 0.75
        assert m["recall"] == 0.3
        assert round(m["f1"], 4) == round(2 * 0.75 * 0.3 / 1.05, 4)


class TestParseJudgeJson:
    def test_pure_json(self):
        assert parse_judge_json('{"score": 4, "reason": "ok"}')["score"] == 4

    def test_markdown_fence(self):
        out = parse_judge_json('```json\n{"score": 5, "reason": "good"}\n```')
        assert out["score"] == 5

    def test_text_with_json(self):
        out = parse_judge_json('评分结果：{"score": 3, "reason": "一般"}')
        assert out["score"] == 3

    def test_invalid(self):
        assert parse_judge_json("not json at all") is None
        assert parse_judge_json("") is None

    def test_list_not_dict(self):
        assert parse_judge_json("[1,2,3]") is None


class TestLlmJudge:
    def _judge_ok(self, system, user):
        return '{"score": 4, "reason": "相关"}'

    def test_scores_ok(self):
        r = llm_judge(self._judge_ok, "样本", "相关性、完整性")
        assert r["score"] == 4 and r["parsed"] is True
        assert r["reason"] == "相关"

    def test_system_prompt_contains_criteria(self):
        captured = {}

        def spy(system, user):
            captured["system"] = system
            captured["user"] = user
            return '{"score": 5, "reason": ""}'

        llm_judge(spy, "样本x", "格式规范、无幻觉")
        assert "格式规范、无幻觉" in captured["system"]
        assert "样本x" in captured["user"]

    def test_score_clamped(self):
        def judge(system, user):
            return '{"score": 99, "reason": ""}'

        assert llm_judge(judge, "s", "c")["score"] == 5

    def test_retry_on_garbage(self):
        calls = []

        def flaky(system, user):
            calls.append(1)
            return "oops" if len(calls) == 1 else '{"score": 2, "reason": "r"}'

        r = llm_judge(flaky, "s", "c", max_retries=1)
        assert r["parsed"] is True and r["score"] == 2

    def test_fail_after_retries(self):
        def always_bad(system, user):
            return "garbage"

        r = llm_judge(always_bad, "s", "c", max_retries=1)
        assert r["parsed"] is False and r["score"] == 0

    def test_judge_exception_handled(self):
        def boom(system, user):
            raise RuntimeError("judge down")

        r = llm_judge(boom, "s", "c")
        assert r["parsed"] is False and r["score"] == 0


class TestJudgeAgreement:
    """judge 与人工标注的一致性：准确率 + Cohen's kappa（校准 LLM-as-judge 的 ground truth）。"""

    def test_perfect_agreement(self):
        r = judge_agreement([1, 2, 3, 4], [1, 2, 3, 4])
        assert r["n"] == 4 and r["accuracy"] == 1.0 and r["kappa"] == 1.0

    def test_complete_disagreement(self):
        # 双方各自完全一致但互不重叠 → po=0, pe=0 → kappa=0（Cohen's kappa 特性）
        r = judge_agreement([0, 0, 0, 0], [5, 5, 5, 5])
        assert r["accuracy"] == 0.0 and r["kappa"] == 0.0

    def test_tolerance_counts_near_miss_as_agree(self):
        labels = [3, 3, 3, 3]
        scores = [2, 3, 4, 3]
        assert judge_agreement(labels, scores, tolerance=1)["accuracy"] == 1.0
        assert judge_agreement(labels, scores, tolerance=0)["accuracy"] == 0.5

    def test_no_variance_kappa_undefined(self):
        # 无分歧（pe==1）→ kappa 不可定义返回 None，准确率仍 1.0
        r = judge_agreement([2, 2], [2, 2])
        assert r["accuracy"] == 1.0 and r["kappa"] is None

    def test_empty_or_mismatched(self):
        assert judge_agreement([], [])["accuracy"] is None
        assert judge_agreement([1, 2], [1])["n"] == 2 and judge_agreement([1, 2], [1])["kappa"] is None
