"""
graph/agents.py — 编排级 Agent 实现（Supervisor 多智能体模式）

把 graph/nodes.py 的 8 个图节点函数包装为显式 Agent，每个 Agent 有独立
职责（role）与行为声明：

  ScoutAgent           侦察兵：分析种子站点 → 站点画像 + 初始任务计划(plan)
  NavigateAgent        领航员：提取导航链接填充 BFS 队列，并把栏目清单并入计划
  FetchExtractAgent    执行者：抓取页面 + 规则清洗 + 落盘（确定性优先）
  EvaluateAgent        审查者：评估爬取质量，对照任务计划检查完成度，决定下一步
  ConfigAdjustAgent    调整者：按评估建议调整爬虫配置并重抓（上限 3 次）
  CodeGenAgent         规则生成者：LLM 生成站点定制清洗规则（最后保底）
  MediaProcessorAgent  媒体处理者：图片过滤 / 外链化
  StorageAgent         存储者：结果落盘 CSV + 兜底重建

职责边界（Supervisor 模式）：
  - workflow 是监督者，负责编排与条件路由
  - EvaluateAgent 是审查 Agent：质量不过 → 交给 AdjustAgent 或 CodeGenAgent，
    通过 → 放行 MediaProcessorAgent / StorageAgent
  - 每个 Agent 的决策会被 TraceRecorder 记录（可复现、可调试）
"""

from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import urlparse

from agents.base import AgentContext, BaseAgent
from .nodes import (
    scout_node,
    navigate_node,
    fetch_extract_node,
    evaluate_node,
    config_adjust_node,
    code_gen_node,
    media_processor_node,
    storage_node,
)
from schemas import agent_logger


# ============================================================================
# 任务计划（Plan-and-Execute）
# ============================================================================

def _derive_plan(profile: Dict[str, Any], sections: List[str]) -> Dict[str, Any]:
    """由站点画像推导初始任务计划。

    plan 字段结构：
      {
        "status": "planned | navigated | evaluated",
        "steps": [...],                 # 预期执行步骤
        "site_type": str,               # 站点类型（静态/JS模板/RuiQiCMS...）
        "needs_js_render": bool,        # 是否需要 Playwright 渲染
        "template_hints": [...],        # 模板特征提示
        "expected_sections": [...],     # 预期栏目清单（navigate 后补充）
      }
    """
    return {
        "status": "planned",
        "steps": ["scout", "navigate", "fetch_extract", "evaluate", "media", "storage"],
        "site_type": str(profile.get("site_type", "")),
        "needs_js_render": bool(profile.get("needs_js_render", False)),
        "template_hints": list(profile.get("template_hints") or []),
        "expected_sections": list(sections),
    }


def _review_plan(plan: Dict[str, Any], evaluation: Dict[str, Any],
                 stats: Dict[str, int]) -> Dict[str, Any]:
    """EvaluateAgent 对照任务计划检查完成度（Plan-and-Execute 落地点）。

    产出评审结论，供 trace 与后续决策使用：
      {"completed": [...], "pending": [...], "passed": bool, "quality_gap": str}
    """
    steps = list(plan.get("steps", []))
    done: set = set()

    if stats.get("scouted", 0) > 0:
        done.add("scout")
    if stats.get("fetched", 0) > 0 or plan.get("status") == "navigated":
        done.update(["navigate", "fetch_extract"])
    if evaluation:
        done.add("evaluate")
    if stats.get("saved", 0) > 0:
        done.update(["media", "storage"])

    pending = [s for s in steps if s not in done]
    passed = bool(evaluation.get("passed")) if evaluation else bool(pending) is False

    quality_gap = ""
    if not passed and evaluation:
        issues = evaluation.get("issues") or []
        quality_gap = ";".join(
            str(i.get("type", "")) for i in issues[:3]
        )

    return {
        "completed": [s for s in steps if s in done],
        "pending": pending,
        "passed": passed,
        "quality_gap": quality_gap,
    }


# ============================================================================
# Agent 1: ScoutAgent — 侦察兵
# ============================================================================

class ScoutAgent(BaseAgent):
    name = "scout"
    role = "侦察兵"
    description = "分析种子站点，产出站点画像(SiteProfile)与初始任务计划(plan)"
    system_prompt = (
        "你是爬虫系统的侦察 Agent。分析种子 URL，判断站点技术栈"
        "（静态 / JS 模板 / 可视化建站）、是否需要渲染、正文容器特征，"
        "并产出后续任务的执行计划。"
    )

    async def run_impl(self, state: Dict[str, Any]) -> Dict[str, Any]:
        result = await scout_node(state)
        if result.get("error"):
            return result
        profile = result.get("site_profile") or {}

        # ★ 经验记忆：历史学习过该站点 → 用记忆修正画像（站点类型 / JS / 模板特征），
        #   避免重复分析，也让跨 run 配置更稳定（冷启动 → 热启动）。
        memory = getattr(self.ctx, "memory", None)
        if memory is not None:
            netloc = urlparse(state.get("seed_url", "")).netloc
            pattern = memory.get_site_pattern(netloc)
            if pattern and pattern.get("site_type"):
                profile = {
                    **profile,
                    "site_type": pattern["site_type"],
                    "needs_js_render": pattern["needs_js_render"],
                    "extra": {
                        **profile.get("extra", {}),
                        "memory_template_hints": pattern.get("template_hints", []),
                    },
                }
                agent_logger.info(
                    f"[Agent::scout] 经验记忆命中 | {netloc} | "
                    f"type={pattern['site_type']} | js={pattern['needs_js_render']}"
                )
                self.trace.record(
                    self.name, "memory_hit",
                    netloc=netloc,
                    site_type=pattern["site_type"],
                    template_hints=pattern.get("template_hints", []),
                )

        plan = _derive_plan(profile, [])
        result["plan"] = plan
        result["site_profile"] = profile

        # ★ Tool 层：通过工具注册表调用注入弱检测（标题是不可信数据，真实使用）
        try:
            title = str(profile.get("title", ""))
            hint = self.ctx.tools.call("detect_injection", content=title)
            if hint:
                agent_logger.warning(f"[Agent::scout] 种子标题疑似注入提示: {hint!r}")
            self.trace.record(
                self.name, "tool_call",
                tool="detect_injection", hit=bool(hint), tools=self.ctx.tools.names(),
            )
        except Exception as e:
            agent_logger.warning(f"[Agent::scout] 工具调用失败: {e}")
        self.trace.record(
            self.name, "plan",
            site_type=plan["site_type"],
            needs_js_render=plan["needs_js_render"],
            template_hints=plan["template_hints"],
        )
        agent_logger.info(
            f"[Agent::scout] 计划产出 | type={plan['site_type']} | "
            f"js={plan['needs_js_render']} | hints={plan['template_hints']}"
        )
        return result

    def _summarize_decision(self, result: Dict[str, Any]) -> str:
        profile = result.get("site_profile") or {}
        return (
            f"站点类型={profile.get('site_type', '')} "
            f"需JS渲染={profile.get('needs_js_render', False)}"
        )


# ============================================================================
# Agent 2: NavigateAgent — 领航员
# ============================================================================

class NavigateAgent(BaseAgent):
    name = "navigate"
    role = "领航员"
    description = "提取首页导航链接填充 BFS 队列，并把栏目清单补充进任务计划"
    system_prompt = (
        "你是爬虫系统的领航 Agent。分析首页导航结构，提取子链接并按"
        "导航路径分类入队，同时把发现的一级栏目清单回填到任务计划。"
    )

    async def run_impl(self, state: Dict[str, Any]) -> Dict[str, Any]:
        result = await navigate_node(state)
        if result.get("error"):
            return result
        nav_mapping = result.get("nav_mapping") or {}
        plan = dict(state.get("plan") or {})
        plan["expected_sections"] = list(nav_mapping.keys())
        plan["status"] = "navigated"
        result["plan"] = plan
        self.trace.record(
            self.name, "navigate",
            enqueued=len(result.get("queue", [])),
            sections=len(plan["expected_sections"]),
            sections_sample=list(plan["expected_sections"])[:5],
        )
        agent_logger.info(
            f"[Agent::navigate] 计划更新 | 栏目={len(plan['expected_sections'])} 个 | "
            f"入队={len(result.get('queue', []))}"
        )
        return result

    def _summarize_decision(self, result: Dict[str, Any]) -> str:
        return f"入队 {len(result.get('queue', []))} 链接 | 栏目 {len(result.get('nav_mapping') or {})} 个"


# ============================================================================
# Agent 3: FetchExtractAgent — 执行者
# ============================================================================

class FetchExtractAgent(BaseAgent):
    name = "fetch_extract"
    role = "执行者"
    description = "抓取页面 + 规则清洗 + 落盘（确定性优先的默认执行者）"
    system_prompt = (
        "你是爬虫系统的执行 Agent。对队列中的每个 URL 完成抓取、反爬检测、"
        "规则清洗与本地落盘；规则引擎无法达标时才触发降级链路。"
    )

    async def run_impl(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return await fetch_extract_node(state)

    def _summarize_decision(self, result: Dict[str, Any]) -> str:
        stats = result.get("stats") or {}
        return (
            f"fetched={stats.get('fetched', 0)} saved={stats.get('saved', 0)} "
            f"failed={stats.get('failed', 0)}"
        )


# ============================================================================
# Agent 4: EvaluateAgent — 审查者
# ============================================================================

class EvaluateAgent(BaseAgent):
    name = "evaluate"
    role = "审查者"
    description = "LLM/启发式评估爬取质量，对照任务计划检查完成度，决定下一步"
    system_prompt = (
        "你是爬虫系统的审查 Agent。对执行者的产出做质量评估（正文完整度、"
        "噪音比例、链接覆盖面），对照任务计划检查完成度，给出通过/调整建议。"
    )

    async def run_impl(self, state: Dict[str, Any]) -> Dict[str, Any]:
        result = await evaluate_node(state)
        if result.get("error"):
            return result
        evaluation = result.get("evaluation") or {}
        plan = state.get("plan") or {}
        stats = state.get("stats") or {}
        review = _review_plan(plan, evaluation, stats)
        self.trace.record(
            self.name, "review",
            source=result.get("eval_source", "?"),
            passed=evaluation.get("passed"),
            score=evaluation.get("score"),
            issue_types=[str(i.get("type", "")) for i in (evaluation.get("issues") or [])][:5],
            completed=review["completed"],
            pending=review["pending"],
            quality_gap=review["quality_gap"],
        )
        agent_logger.info(
            f"[Agent::evaluate] 审查结论 | source={result.get('eval_source', '?')} | "
            f"passed={review['passed']} | score={evaluation.get('score')} | "
            f"待完成={review['pending']} | gap={review['quality_gap'] or '-'}"
        )
        return result

    def _summarize_decision(self, result: Dict[str, Any]) -> str:
        evaluation = result.get("evaluation") or {}
        return (
            f"passed={evaluation.get('passed')} "
            f"score={evaluation.get('score')} "
            f"issues={len(evaluation.get('issues') or [])}"
        )


# ============================================================================
# Agent 5: ConfigAdjustAgent — 调整者
# ============================================================================

class ConfigAdjustAgent(BaseAgent):
    name = "config_adjust"
    role = "调整者"
    description = "按评估建议调整爬虫配置（UA/渲染/延迟）并触发重抓（上限 3 次）"
    system_prompt = (
        "你是爬虫系统的调整 Agent。根据审查 Agent 的评估建议，调整抓取配置"
        "（User-Agent / JS 渲染 / 请求延迟 / 反爬降级），让执行者重试。"
    )

    async def run_impl(self, state: Dict[str, Any]) -> Dict[str, Any]:
        result = await config_adjust_node(state)
        if result.get("error"):
            return result
        self.trace.record(
            self.name, "adjust",
            adjustment_count=result.get("adjustment_count", 0),
        )
        return result

    def _summarize_decision(self, result: Dict[str, Any]) -> str:
        return f"调整次数={result.get('adjustment_count', 0)}"


# ============================================================================
# Agent 6: CodeGenAgent — 规则生成者
# ============================================================================

class CodeGenAgent(BaseAgent):
    name = "code_gen"
    role = "规则生成者"
    description = "LLM 生成站点定制清洗规则（仅 CSS 选择器，防注入）作为最后保底"
    system_prompt = (
        "你是爬虫系统的规则生成 Agent。基于失败页面样本生成站点定制的提取规则。"
        "安全约束：只允许输出 CSS 选择器结构，禁止生成任何可执行代码。"
    )

    async def run_impl(self, state: Dict[str, Any]) -> Dict[str, Any]:
        result = await code_gen_node(state)
        if result.get("error"):
            return result
        rules = result.get("extraction_rules") or {}
        self.trace.record(
            self.name, "generate",
            confidence=rules.get("confidence"),
            selectors={
                k: len(v) for k, v in rules.items()
                if isinstance(v, list) and k in ("content_selectors", "title_selectors",
                                                 "image_selectors", "remove_selectors")
            },
        )
        return result

    def _summarize_decision(self, result: Dict[str, Any]) -> str:
        rules = result.get("extraction_rules") or {}
        return f"confidence={rules.get('confidence')}"


# ============================================================================
# Agent 7: MediaProcessorAgent — 媒体处理者
# ============================================================================

class MediaProcessorAgent(BaseAgent):
    name = "media_processor"
    role = "媒体处理者"
    description = "图片过滤（装饰图/二维码）与外链化处理"
    system_prompt = "你是爬虫系统的媒体处理 Agent，负责页面图片的过滤与规范化。"

    async def run_impl(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return await media_processor_node(state)

    def _summarize_decision(self, result: Dict[str, Any]) -> str:
        return f"处理行数={len(result.get('media_results') or [])}"


# ============================================================================
# Agent 8: StorageAgent — 存储者
# ============================================================================

class StorageAgent(BaseAgent):
    name = "storage"
    role = "存储者"
    description = "将结果落盘 CSV（去重 + 兜底重建）"
    system_prompt = "你是爬虫系统的存储 Agent，负责结果的规范化落盘与一致性。"

    async def run_impl(self, state: Dict[str, Any]) -> Dict[str, Any]:
        stats = state.get("stats") or {}
        result = await storage_node(state)
        self.trace.record(
            self.name, "store",
            stats={k: stats.get(k) for k in ("saved", "skipped", "duplicate", "failed")},
        )
        # ★ 经验记忆：成功爬完 → 写入站点学习模式，供同站点下次侦察直接命中复用
        if stats.get("saved", 0) > 0:
            try:
                memory = self.ctx.memory
                netloc = urlparse(state.get("seed_url", "")).netloc
                plan = state.get("plan") or {}
                memory.save_site_pattern(
                    netloc=netloc,
                    site_type=str(plan.get("site_type", "")),
                    needs_js_render=bool(plan.get("needs_js_render", False)),
                    template_hints=list(plan.get("template_hints") or []),
                    stats={k: stats.get(k) for k in ("saved", "skipped", "duplicate", "failed")},
                )
                self.trace.record(
                    self.name, "memory_save",
                    netloc=netloc, saved=stats.get("saved", 0),
                )
                agent_logger.info(
                    f"[Agent::storage] 站点学习模式已写入 | {netloc} | "
                    f"saved={stats.get('saved', 0)}"
                )
            except Exception as e:
                agent_logger.warning(f"[StorageAgent] 保存站点模式失败: {e}")
        return result

    def _summarize_decision(self, result: Dict[str, Any]) -> str:
        return ""


def build_agents(ctx: AgentContext) -> Dict[str, BaseAgent]:
    """构建全部编排级 Agent 实例，按节点名索引。"""
    agents = [
        ScoutAgent(ctx),
        NavigateAgent(ctx),
        FetchExtractAgent(ctx),
        EvaluateAgent(ctx),
        ConfigAdjustAgent(ctx),
        CodeGenAgent(ctx),
        MediaProcessorAgent(ctx),
        StorageAgent(ctx),
    ]
    return {a.name: a for a in agents}
