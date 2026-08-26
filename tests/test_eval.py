"""tests/test_eval.py — Agent 评估体系：P/R/F1 + LLM-as-judge 单元测试。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.eval import compute_prf, llm_judge, parse_judge_json


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
