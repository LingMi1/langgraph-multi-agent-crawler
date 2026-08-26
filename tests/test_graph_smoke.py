"""tests/test_graph_smoke.py — 图装配离线冒烟测试（不联网）。

验证 Supervisor 多智能体架构的关键装配点：
  - build_agents 产出 8 个编排级 Agent（名称/职责齐全）
  - build_crawler_graph 注册全部节点并成功 compile
  - Agent 列表与 _AGENT_NODE_MAP 一一对应（缺 Agent 直接抛 KeyError）
"""

from agents.base import AgentContext, TraceRecorder
from graph import build_agents, build_crawler_graph

EXPECTED_AGENTS = {
    "scout",
    "navigate",
    "fetch_extract",
    "evaluate",
    "config_adjust",
    "code_gen",
    "media_processor",
    "storage",
}


def test_build_agents_produces_8_agents(tmp_path):
    ctx = AgentContext(trace=TraceRecorder(output_dir=str(tmp_path)))
    agents = build_agents(ctx)
    assert set(agents.keys()) == EXPECTED_AGENTS
    # 职责声明齐全（面试叙事：每个 Agent 有 role/description）
    for name, agent in agents.items():
        assert agent.role, f"{name} 缺 role"
        assert agent.description, f"{name} 缺 description"


def test_graph_compiles_with_all_nodes(tmp_path):
    ctx = AgentContext(trace=TraceRecorder(output_dir=str(tmp_path)))
    agents = build_agents(ctx)
    graph = build_crawler_graph(agents)
    compiled = graph.compile()
    assert compiled is not None


def test_missing_agent_raises_keyerror(tmp_path):
    ctx = AgentContext(trace=TraceRecorder(output_dir=str(tmp_path)))
    agents = build_agents(ctx)
    del agents["storage"]  # 模拟缺 Agent
    try:
        build_crawler_graph(agents)
    except KeyError as e:
        assert "storage" in str(e)
    else:
        raise AssertionError("缺少编排级 Agent 时应抛 KeyError")
