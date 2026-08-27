"""
graph/react_takeover.py — 深降级 ReAct 自主接管节点（面试叙事核心）

定位（三层兜底链）：
  传统爬虫（确定性优先）→ LLM 评估/规则生成（关键节点介入）→ 本节点（自主接管）
  前两层全部失败后，Agent 进入 ReAct 模式自主诊断并决策：

      LLM 决策 → 调用行动工具（fetch_page / apply_config）→ 结果回填 → 收敛决策

与 evaluate 节点 FC 路径（agents/react.py + quality_judge）的区别：
  - evaluate 的 quality_judge 是"只读"分析工具（拿客观分再裁决，不动状态）
  - 本节点的 fetch_page / apply_config 是"行动"工具（Agent 真正动手：重抓/改配置）

护栏（深降级不能成为新的失控源）：
  - react_attempted 一次性触发，绝不进入第二轮（防死循环）
  - FunctionCallingLoop max_rounds=4（成本兜底）
  - 无 LLM / 决策 JSON 解析失败 → 保守放弃（giveup），不盲目重试
  - 每次工具调用的入参/出参均落 FunctionCallingLoop.trace（可复核）
  - apply_config 只接受白名单字段（needs_js_render / user_agent / request_delay / ...）
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

from agents.tools import Tool, ToolRegistry
from schemas import agent_logger

from .state import CrawlerState


# ============================================================================
# 行动工具 1：fetch_page — 侦察式抓取
# ============================================================================

def _exec_fetch_page(url: str) -> dict:
    """对 URL 做一次侦察式 HTTP 抓取（15s 超时），返回状态码/长度/标题预览。"""
    import httpx
    try:
        resp = httpx.get(
            url,
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SiteCrawlerAgent/1.0)"},
        )
        text = resp.text
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
        return {
            "status": resp.status_code,
            "final_url": str(resp.url),
            "content_type": str(resp.headers.get("content-type", "")),
            "content_len": len(text),
            "title": title,
            "error": "",
        }
    except Exception as e:  # noqa: BLE001 — 侦察失败必须回填可诊断信息
        return {"status": 0, "final_url": "", "content_type": "",
                "content_len": 0, "title": "", "error": str(e)[:200]}


# ============================================================================
# 行动工具 2：apply_config — 生成新抓取配置（白名单字段）
# ============================================================================

def _exec_apply_config(needs_js_render: Any = None, user_agent: str = "",
                       request_delay: Any = None,
                       use_system_chrome: Any = None) -> dict:
    """生成配置片段：只接受与 CrawlerConfig 对齐的白名单字段。"""
    cfg: Dict[str, Any] = {}
    if needs_js_render is not None:
        cfg["needs_js_render"] = bool(needs_js_render)
    if user_agent:
        cfg["user_agent"] = str(user_agent)[:256]
    if request_delay is not None:
        try:
            cfg["request_delay"] = min(10.0, max(0.0, float(request_delay)))
        except (TypeError, ValueError):
            pass
    if use_system_chrome is not None:
        cfg["use_system_chrome"] = bool(use_system_chrome)
    return cfg


def react_tools() -> ToolRegistry:
    """深降级接管工具集：内置分析工具 + 2 个行动工具。"""
    reg = ToolRegistry.builtin()
    reg.register(Tool(
        "fetch_page",
        "对指定 URL 做一次侦察式抓取（HTTP 直连，15s 超时），返回状态码/最终URL/"
        "内容长度/标题预览；用于确认站点当前是否可达、页面是否空壳或被反爬拦截。",
        {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        _exec_fetch_page,
    ))
    reg.register(Tool(
        "apply_config", "生成新的抓取配置片段（白名单字段）：needs_js_render=启用JS渲染；"
        "user_agent=自定义UA；request_delay=请求间延迟秒数；use_system_chrome=使用系统Chrome。"
        "返回合并后的配置片段，供后续重抓使用。",
        {
            "type": "object",
            "properties": {
                "needs_js_render": {"type": "boolean"},
                "user_agent": {"type": "string"},
                "request_delay": {"type": "number"},
                "use_system_chrome": {"type": "boolean"},
            },
        },
        _exec_apply_config,
    ))
    return reg


# ============================================================================
# 决策解析（LLM 最终输出 → retry / giveup + 配置）
# ============================================================================

_ALLOWED_CFG_FIELDS = {
    "needs_js_render", "user_agent", "request_delay",
    "use_system_chrome", "extra_headers",
}


def _parse_decision(answer: str):
    """解析 LLM 最终决策 JSON；任何失败 → 保守 (giveup, 原文摘要, {})。"""
    if not answer:
        return "giveup", "无输出", {}
    text = answer.strip()
    obj: Dict[str, Any] = {}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return "giveup", text[:120], {}
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return "giveup", text[:120], {}
    decision = str(obj.get("decision", "giveup")).strip().lower()
    cfg = obj.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = {k: v for k, v in cfg.items() if k in _ALLOWED_CFG_FIELDS}
    reason = str(obj.get("reason", ""))[:200] or text[:120]
    return ("retry" if decision == "retry" else "giveup", reason, cfg)


# ============================================================================
# 深降级接管节点
# ============================================================================

async def react_takeover_node(state: CrawlerState) -> dict:
    """确定性链路全失败后的 ReAct 自主接管：诊断 → 行动 → 决策 retry/giveup。"""
    if state.get("react_attempted"):
        # 防御：绝不让接管进入第二轮
        return {"react_attempted": True, "react_decision": "giveup",
                "react_summary": "接管已触发过一次，不再重复"}

    seed_url = state.get("seed_url", "")

    # LLM 惰性构造（与 evaluate 共用同一客户端与 TokenBudget 记账）
    from graph.nodes import _get_llm, _url_key
    llm = _get_llm()
    if llm is None:
        agent_logger.info("[Graph::react] 无 LLM 客户端，跳过接管（保守落盘）")
        return {"react_attempted": True, "react_decision": "giveup",
                "react_summary": "无 LLM 客户端，跳过接管"}

    from agents.react import FunctionCallingLoop
    loop = FunctionCallingLoop(llm, react_tools(), max_rounds=4)

    stats = state.get("stats") or {}
    evaluation = state.get("evaluation") or {}
    issues = [str(i.get("type", "")) for i in (evaluation.get("issues") or [])[:3]]
    system = (
        "你是爬虫系统的深降级接管 Agent。传统爬虫、配置调整与规则生成均已失败，"
        "现在由你自主诊断并决策。\n"
        "行动工具（用文本标记调用，一次可调多个）：\n"
        "```tool\n{\"name\": \"fetch_page\", \"arguments\": {\"url\": \"目标URL\"}}\n```\n"
        "```tool\n{\"name\": \"apply_config\", \"arguments\": {\"needs_js_render\": true}}\n```\n"
        "诊断思路：1) 用 fetch_page 确认站点可达性/是否空壳/是否被反爬拦截；"
        "2) 若怀疑 JS 渲染或 UA 被拦，用 apply_config 生成新配置；"
        "3) 无法解决时明确放弃，不要编造。\n"
        "最终必须只输出一行 JSON（不要再调用工具）：\n"
        '{"decision": "retry"|"giveup", "reason": "一句话", "config": {可选配置字段}}\n'
        "decision=retry 仅当你拿到明确可行动的配置调整；其余一律 giveup。"
    )
    user = (
        "目标站点: %s\n当前统计: %s\n评估摘要: %s\n已知问题: %s"
        % (seed_url, stats, evaluation.get("summary", ""), issues)
    )
    result = await loop.run([{"role": "user", "content": user}], system=system)
    answer = (result.get("answer") or "").strip()
    decision, reason, cfg = _parse_decision(answer)
    rounds = result.get("rounds", 0)

    if decision == "retry":
        merged = dict(state.get("crawler_config") or {})
        merged.update(cfg)
        agent_logger.info(
            "[Graph::react] 接管决策：重试 | rounds=%d | reason=%s | config=%s",
            rounds, reason, merged,
        )
        return {
            "react_attempted": True,
            "react_decision": "retry",
            "react_summary": reason,
            "crawler_config": merged,
            "adjustment_count": state.get("adjustment_count", 0) + 1,
            "queue": [{"url": seed_url, "depth": 1, "nav_path": [], "is_homepage": True}],
            "seen_url_keys": [_url_key(seed_url)],
            "crawled_results": [],  # 清空，用新配置重新抓取
        }

    agent_logger.info("[Graph::react] 接管决策：放弃 | rounds=%d | reason=%s", rounds, reason)
    return {"react_attempted": True, "react_decision": "giveup",
            "react_summary": reason or answer[:120]}
