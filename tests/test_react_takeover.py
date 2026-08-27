"""tests/test_react_takeover.py — 深降级 ReAct 接管链路测试

方法论（与 test_react_loop.py 一致）：
  - FakeLLM 桩掉真实模型，只测"路由 → 接管节点 → 决策解析 → 状态变更"链路
  - 无 LLM / 决策解析失败 → 保守 giveup（深降级不能成为新的失控源）
  - apply_config 白名单过滤：LLM 输出中的未知字段必须被剥离
"""

import asyncio

from graph.nodes import route_after_evaluate, route_after_react
from graph.react_takeover import (
    _exec_apply_config,
    _exec_fetch_page,
    _parse_decision,
    react_takeover_node,
    react_tools,
)


class FakeLLM:
    """桩模型：按脚本依次返回预设响应（dict 或 str）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def ainvoke(self, messages, **kw):
        self.calls += 1
        return self.script.pop(0)


def _failed_eval_state(**over):
    state = {
        "seed_url": "http://www.example.com/",
        "evaluation": {"passed": False, "score": 0.3, "summary": "正文过短",
                       "issues": [{"type": "content_quality", "severity": "warning",
                                   "description": "", "affected_pages": 1}]},
        "adjustment_count": 3,
        "generation_attempted": True,
        "stats": {"fetched": 2, "saved": 0, "failed": 2},
        "crawler_config": {"user_agent": "", "needs_js_render": False,
                           "request_delay": 1.0},
    }
    state.update(over)
    return state


# ============================================================================
# 路由：evaluate → react / media_processor
# ============================================================================

def test_route_eval_to_react_when_generation_done_and_not_attempted():
    assert route_after_evaluate(_failed_eval_state()) == "react_node"


def test_route_eval_to_media_after_react_attempted():
    state = _failed_eval_state(react_attempted=True)
    assert route_after_evaluate(state) == "media_processor_node"


def test_route_eval_passed_skips_react():
    state = _failed_eval_state()
    state["evaluation"]["passed"] = True
    assert route_after_evaluate(state) == "media_processor_node"


def test_route_eval_less_than_max_adjust_skips_react():
    state = _failed_eval_state(adjustment_count=1)
    assert route_after_evaluate(state) == "config_adjust_node"


# ============================================================================
# 路由：react → navigate / media_processor
# ============================================================================

def test_route_react_retry_to_navigate():
    assert route_after_react({"react_decision": "retry"}) == "navigate_node"


def test_route_react_giveup_to_media():
    assert route_after_react({"react_decision": "giveup"}) == "media_processor_node"
    assert route_after_react({}) == "media_processor_node"


# ============================================================================
# 决策解析（_parse_decision）
# ============================================================================

def test_parse_decision_retry_with_config():
    answer = '{"decision": "retry", "reason": "需JS渲染", "config": {"needs_js_render": true, "request_delay": 2.5}}'
    decision, reason, cfg = _parse_decision(answer)
    assert decision == "retry"
    assert cfg == {"needs_js_render": True, "request_delay": 2.5}


def test_parse_decision_filters_unknown_config_fields():
    answer = '{"decision": "retry", "config": {"needs_js_render": true, "evil_flag": "x", "exec": "rm -rf"}}'
    decision, _, cfg = _parse_decision(answer)
    assert decision == "retry"
    assert cfg == {"needs_js_render": True}
    assert "evil_flag" not in cfg and "exec" not in cfg


def test_parse_decision_embedded_in_prose():
    answer = '诊断完成，最终决策：{"decision": "giveup", "reason": "站点已下线"}'
    decision, reason, _ = _parse_decision(answer)
    assert decision == "giveup"
    assert "下线" in reason


def test_parse_decision_malformed_defaults_giveup():
    decision, reason, cfg = _parse_decision("完全无法解析的输出")
    assert decision == "giveup"
    assert cfg == {}


def test_parse_decision_empty_defaults_giveup():
    assert _parse_decision("")[0] == "giveup"


# ============================================================================
# 行动工具
# ============================================================================

def test_apply_config_whitelist_and_clamp():
    cfg = _exec_apply_config(needs_js_render=True, user_agent="UA/1.0",
                             request_delay=99.0, use_system_chrome=True)
    assert cfg == {"needs_js_render": True, "user_agent": "UA/1.0",
                   "request_delay": 10.0, "use_system_chrome": True}  # 99 → 10 封顶


def test_apply_config_ignores_bad_types():
    cfg = _exec_apply_config(request_delay="not-a-number")
    assert "request_delay" not in cfg


def test_fetch_page_unreachable_returns_error():
    # 只连本地拒绝端口，不触网
    r = _exec_fetch_page("http://127.0.0.1:9/")
    assert r["status"] == 0
    assert r["error"]


def test_react_tools_include_action_tools():
    names = react_tools().names()
    assert "fetch_page" in names and "apply_config" in names
    assert "quality_judge" in names  # 仍保留内置分析工具


# ============================================================================
# 接管节点（无 LLM → 保守 giveup）
# ============================================================================

def test_react_node_without_llm_gives_up(monkeypatch):
    monkeypatch.setattr("graph.nodes._get_llm", lambda: None)
    result = asyncio.run(react_takeover_node(_failed_eval_state()))
    assert result["react_attempted"] is True
    assert result["react_decision"] == "giveup"


def test_react_node_already_attempted_never_reruns(monkeypatch):
    monkeypatch.setattr("graph.nodes._get_llm", lambda: None)
    state = _failed_eval_state(react_attempted=True)
    result = asyncio.run(react_takeover_node(state))
    assert result["react_decision"] == "giveup"
    assert "一次" in result["react_summary"]


# ============================================================================
# 接管节点（有 LLM → retry：行动工具 → 收敛决策 → 状态变更）
# ============================================================================

def test_react_node_retry_applies_config_and_reenqueues(monkeypatch):
    script = [
        {"content": '```tool\n{"name": "apply_config", "arguments": {"needs_js_render": true, "request_delay": 2.0}}\n```'},
        '{"decision": "retry", "reason": "站点为JS模板，需渲染", "config": {"needs_js_render": true, "request_delay": 2.0, "evil": "x"}}',
    ]
    monkeypatch.setattr("graph.nodes._get_llm", lambda: FakeLLM(script))
    result = asyncio.run(react_takeover_node(_failed_eval_state()))
    assert result["react_decision"] == "retry"
    assert result["react_attempted"] is True
    # 配置合并 + 白名单过滤
    assert result["crawler_config"]["needs_js_render"] is True
    assert result["crawler_config"]["request_delay"] == 2.0
    assert "evil" not in result["crawler_config"]
    # 重入队：从种子重抓，清空结果
    assert result["queue"][0]["url"] == "http://www.example.com/"
    assert result["queue"][0]["is_homepage"] is True
    assert result["crawled_results"] == []
    assert result["adjustment_count"] == 4


def test_react_node_giveup_when_model_gives_up(monkeypatch):
    script = ['{"decision": "giveup", "reason": "站点 403 且无 JS 渲染价值"}']
    monkeypatch.setattr("graph.nodes._get_llm", lambda: FakeLLM(script))
    result = asyncio.run(react_takeover_node(_failed_eval_state()))
    assert result["react_decision"] == "giveup"
    assert "403" in result["react_summary"]
    assert "crawler_config" not in result  # giveup 不碰配置


def test_react_node_script_sequence_counts_calls(monkeypatch):
    script = [
        '{"decision": "giveup", "reason": "放弃"}',
    ]
    fake = FakeLLM(script)
    monkeypatch.setattr("graph.nodes._get_llm", lambda: fake)
    asyncio.run(react_takeover_node(_failed_eval_state()))
    assert fake.calls == 1
