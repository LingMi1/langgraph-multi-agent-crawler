"""tests/test_graph_routing.py — Supervisor 核心决策路由单测（纯函数、离线、零 mock）。

覆盖 graph/nodes.py 的三个条件路由函数的全部分支（README 架构图中的裁决边）：
  - route_after_fetch   ：BFS 循环 / 队列耗尽进评估 / 出错直落盘
  - route_after_evaluate：通过 / 调整(<3) / 规则生成 / ReAct 接管 / 保守落盘 / 出错
  - route_after_react   ：接管重试 / 放弃落盘

路由是整个 Supervisor 图的"裁决中枢"——此前只有图装配冒烟没有分支级断言。
"""

from graph.nodes import route_after_evaluate, route_after_fetch, route_after_react


# ── route_after_fetch：fetch_extract 之后的 BFS 循环裁决 ──


def test_fetch_route_error_goes_to_storage():
    """抓取阶段出错 → 不再循环，直接落盘保数据。"""
    assert route_after_fetch({"error": "boom", "queue": ["a"]}) == "storage_node"


def test_fetch_route_queue_nonempty_loops():
    """队列非空且无错 → 回到 fetch_extract_node 继续 BFS。"""
    assert route_after_fetch({"queue": ["/a", "/b"]}) == "fetch_extract_node"


def test_fetch_route_queue_empty_goes_to_evaluate():
    """队列耗尽 → 进入评估（plan-and-execute 的审查入口）。"""
    assert route_after_fetch({"queue": []}) == "evaluate_node"


# ── route_after_evaluate：评估之后的五路裁决（升级阶梯） ──


def test_evaluate_route_error_goes_to_storage():
    assert route_after_evaluate({"error": "x"}) == "storage_node"


def test_evaluate_route_passed_goes_to_media():
    assert route_after_evaluate({"evaluation": {"passed": True}}) == "media_processor_node"


def test_evaluate_route_below_adjust_cap_goes_to_config_adjust():
    """未通过但调整次数 < 3 → 调整配置重抓（最便宜的修复手段优先）。"""
    assert route_after_evaluate({"evaluation": {"passed": False}, "adjustment_count": 2}) == "config_adjust_node"


def test_evaluate_route_after_adjust_cap_goes_to_code_gen():
    """调整耗尽且还没生成过规则 → LLM 生成站点规则（最后保底）。"""
    state = {"evaluation": {"passed": False}, "adjustment_count": 3, "generation_attempted": False}
    assert route_after_evaluate(state) == "code_gen_node"


def test_evaluate_route_rules_failed_goes_to_react():
    """规则已生成仍失败 → 深降级 ReAct 自主接管（一次性）。"""
    state = {"evaluation": {"passed": False}, "adjustment_count": 3, "generation_attempted": True,
             "react_attempted": False}
    assert route_after_evaluate(state) == "react_node"


def test_evaluate_route_all_exhausted_goes_to_media():
    """调整 / 规则 / 接管全部尝试过 → 保守落盘，绝不丢数据。"""
    state = {"evaluation": {"passed": False}, "adjustment_count": 3, "generation_attempted": True,
             "react_attempted": True}
    assert route_after_evaluate(state) == "media_processor_node"


def test_evaluate_route_defaults_to_pass_when_no_evaluation():
    """无评估结果时默认 passed=True（缺省走媒体处理，防止误入升级阶梯）。"""
    assert route_after_evaluate({}) == "media_processor_node"


# ── route_after_react：深降级接管后的去留裁决 ──


def test_react_route_retry_returns_to_navigate():
    assert route_after_react({"react_decision": "retry"}) == "navigate_node"


def test_react_route_giveup_goes_to_media():
    assert route_after_react({"react_decision": "giveup"}) == "media_processor_node"


def test_react_route_missing_decision_defaults_to_media():
    """无决策字段按放弃处理（保守落盘）。"""
    assert route_after_react({}) == "media_processor_node"
