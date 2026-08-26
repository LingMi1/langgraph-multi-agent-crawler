"""tests/test_fc_eval.py — 核心链路 Function Calling 评估（LLM 调 quality_judge 工具再裁决）。

验证"LLM → 工具 → 收敛裁决"链路 + 失败降级（模型不支持工具标记 / 输出不可解析时
返回 None，由 evaluate_node 降级到纯文本评估）。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph.nodes import _llm_evaluate_fc, _parse_eval_json
from graph.state import EvaluationResult

_RESULTS = [{"title": "豫花花生油", "html": "<p>简介</p>", "download_img_url": ""}]
_STATS = {"saved": 6, "failed": 0, "skipped": 0, "duplicate": 0}


class FakeLLM:
    """按序返回预设响应；超出后重复最后一个。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def ainvoke(self, messages):
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return r


_TOOL_MARKER = (
    '```tool\n{"name": "quality_judge", "arguments": {"sample": "样本"}}\n```'
)
_VERDICT = (
    '{"passed": true, "score": 0.9, "summary": "质量达标", '
    '"issues": [], "needs_js_render": false, "recommended_ua": ""}'
)


def run(coro):
    return asyncio.run(coro)


class TestFcEvalPath:
    def test_llm_calls_tool_then_verdict(self):
        """LLM 先调 quality_judge 工具，拿到结果后输出最终 JSON 裁决。"""
        llm = FakeLLM([_TOOL_MARKER, _VERDICT])
        ev = run(_llm_evaluate_fc(llm, _RESULTS, _STATS, "hnbn666", "http://x.com"))
        assert isinstance(ev, EvaluationResult)
        assert ev.passed is True and ev.score == 0.9
        assert llm.calls == 2  # 工具轮 + 裁决轮

    def test_loop_actually_executed_tool(self):
        """工具真实执行：quality_judge 是确定性打分（非 mock 返回）。"""
        captured = {}

        class SpyLLM(FakeLLM):
            async def ainvoke(self, messages):
                # 捕获传给工具执行前后的历史，验证工具结果回填
                captured["history"] = [m.get("role") for m in messages]
                return await super().ainvoke(messages)

        llm = SpyLLM([_TOOL_MARKER, _VERDICT])
        ev = run(_llm_evaluate_fc(llm, _RESULTS, _STATS, "s", "u"))
        assert ev is not None
        roles = captured["history"]
        assert "tool" in roles  # 工具结果已作为 tool 消息回填
        assert "assistant" in roles  # 工具调用帧存在

    def test_markdown_verdict(self):
        llm = FakeLLM(["```json\n" + _VERDICT + "\n```"])
        ev = run(_llm_evaluate_fc(llm, _RESULTS, _STATS, "s", "u"))
        assert isinstance(ev, EvaluationResult) and ev.passed is True

    def test_garbage_answer_returns_none(self):
        llm = FakeLLM(["这不是 JSON"])
        assert run(_llm_evaluate_fc(llm, _RESULTS, _STATS, "s", "u")) is None

    def test_no_answer_returns_none(self):
        llm = FakeLLM([""])
        assert run(_llm_evaluate_fc(llm, _RESULTS, _STATS, "s", "u")) is None

    def test_llm_exception_returns_none(self):
        class Boom(FakeLLM):
            async def ainvoke(self, messages):
                raise RuntimeError("模型挂了")

        assert run(_llm_evaluate_fc(Boom([]), _RESULTS, _STATS, "s", "u")) is None


class TestParseEvalJson:
    def test_pure(self):
        assert _parse_eval_json('{"passed": true}')["passed"] is True

    def test_fence(self):
        assert _parse_eval_json('```json\n{"passed": false}\n```')["passed"] is False

    def test_invalid(self):
        assert _parse_eval_json("oops") is None
        assert _parse_eval_json("") is None
