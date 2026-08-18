"""
Supervisor 决策节点 — LLM 驱动的路由决策器 (Phase 3 重构)

职责:
  1. 读取当前 State 摘要（队列大小、已爬取数、错误数）
  2. 调用 LLM 分析状态，输出结构化 JSON 决策
  3. JSON 解析失败时自动降级到规则决策
"""
import json
import re
from typing import Dict, Any
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI
import config
from state import AgentState, agent_logger, log_agent_thought, log_section

_SUPERVISOR_LLM = None


def reset_supervisor_llm():
    """强制重置 Supervisor LLM 实例，使下次调用使用最新的 config 值"""
    global _SUPERVISOR_LLM
    _SUPERVISOR_LLM = None


def _get_supervisor_llm():
    """Supervisor 专用 LLM（temperature=0 保证决策稳定性）"""
    global _SUPERVISOR_LLM
    if _SUPERVISOR_LLM is None:
        import httpx
        _SUPERVISOR_LLM = ChatOpenAI(
            model=config.get_model_name(),
            openai_api_key=config.DEEPSEEK_API_KEY,
            openai_api_base=config.DEEPSEEK_BASE_URL,
            temperature=0,
            max_tokens=1024,
            request_timeout=60,
            http_client=httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)),
        )
    return _SUPERVISOR_LLM


# ======================================================================
# System Prompt
# ======================================================================

_SUPERVISOR_SYSTEM_PROMPT = """你是一个多智能体系统的 Supervisor（主管），负责分析爬取状态并做出路由决策。

## 你的任务
收到当前系统状态后，输出简洁的 JSON 决策。

## JSON 输出格式（严格遵守）
```json
{"action": "scrape"|"finish", "reasoning": "一句话说明理由", "priority_url": "可选,需优先爬取的URL"}
```

## ★★★ action 字段只能使用 scrape 或 finish，严禁使用其他值！★★★

## 决策规则
- **action=scrape**: 还有 URL 等待爬取，继续派发 Worker
- **action=finish**: 队列为空 OR 已达最大页面数 OR 连续错误过多，结束任务（注意：必须用小写 finish，不能用 END/end/Finish 等其余形式）
- **action=retry**: (保留，暂不使用)

## 终止条件
1. url_queue 为空 (queue_size = 0)
2. crawled 数量达到 max_pages 限制
3. 连续错误 > 5 次

## 注意事项
- 只输出 JSON，不要输出解释性文字
- reasoning 用中文简述理由
- priority_url 可以留空字符串
"""


# ======================================================================
# JSON 解析（鲁棒性）
# ======================================================================

def _parse_supervisor_json(content: str) -> Dict[str, Any]:
    """
    从 LLM 输出中提取 JSON 决策，做多层容错。
    
    容错策略:
      1. 直接 json.loads
      2. 提取 ```json ... ``` 代码块
      3. 提取 { ... } 花括号内容
      4. 降级到规则兜底
    
    Returns: {"action": "scrape"|"finish", "reasoning": str, "priority_url": str}
    """
    result = {"action": "finish", "reasoning": "JSON 解析失败，降级为 finish", "priority_url": ""}

    def _try_parse(text: str) -> Dict:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None

    def _normalize_action(parsed_dict: Dict) -> Dict[str, Any]:
        """将 LLM 可能输出的非标准 action 值标准化为 scrape/finish"""
        action = parsed_dict.get("action", "finish")
        action_lower = str(action).lower()
        # 将 "END" / "end" / "stop" / "done" / "complete" 等结束类词汇映射为 finish
        if action_lower in ("end", "stop", "done", "complete", "exit", "quit", "terminate"):
            action = "finish"
        elif action_lower in ("scrape", "continue", "go", "run", "start", "crawl", "begin"):
            action = "scrape"
        elif action_lower == "finish":
            action = "finish"
        else:
            # 未知值: 根据队列上下文判断
            action = "finish"
        return {"action": action,
                "reasoning": parsed_dict.get("reasoning", "无"),
                "priority_url": parsed_dict.get("priority_url", "")}

    # 策略1: 直接解析
    parsed = _try_parse(content.strip())
    if parsed and "action" in parsed:
        return _normalize_action(parsed)

    # 策略2: 提取代码块
    code_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', content)
    if code_match:
        parsed = _try_parse(code_match.group(1).strip())
        if parsed and "action" in parsed:
            return _normalize_action(parsed)

    # 策略3: 提取最外层花括号
    brace_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', content)
    if brace_match:
        parsed = _try_parse(brace_match.group())
        if parsed and "action" in parsed:
            return _normalize_action(parsed)

    # 策略4: 降级到关键词匹配
    if "scrape" in content.lower() or "继续" in content or "深入" in content:
        return {"action": "scrape", "reasoning": "降级规则: LLM 输出含继续爬取信号", "priority_url": ""}
    if "finish" in content.lower() or "结束" in content or "完成" in content:
        return {"action": "finish", "reasoning": "降级规则: LLM 输出含结束信号", "priority_url": ""}

    # 终极降级
    return result


# ======================================================================
# Supervisor 节点
# ======================================================================

def supervisor_node(state: dict) -> Dict[str, Any]:
    """
    LLM 驱动的 Supervisor 决策节点。
    
    输入: 当前 AgentState
    输出: {supervisor_messages, next_worker, worker_data, worker_results}
    """
    log_section("Supervisor: 分析状态...")

    # 读取当前状态
    base_url = state.get("base_url", "")
    url_queue = state.get("url_queue", [])
    stats = state.get("stats", {})
    error_log = state.get("error_log", [])
    extracted_data = state.get("extracted_data", [])
    visited = state.get("visited", [])
    max_pages = state.get("max_pages", config.MAX_PAGES if hasattr(config, "MAX_PAGES") else 500)

    queue_size = len(url_queue)
    crawled = stats.get("saved", stats.get("total", len(visited)))
    success_count = stats.get("success", 0)
    failed_count = stats.get("failed", 0)
    recent_errors = error_log[-3:] if error_log else []

    # 构建状态摘要
    state_summary = {
        "base_url": base_url,
        "queue_size": queue_size,
        "crawled_pages": crawled,
        "max_pages": max_pages,
        "success": success_count,
        "failed": failed_count,
        "extracted_articles": len(extracted_data),
        "recent_errors": [
            {"url": e.get("url", "")[:60], "msg": e.get("message", "")[:100]}
            for e in recent_errors
        ],
    }

    prompt = f"""当前系统状态:
- 目标站点: {state_summary['base_url']}
- URL 队列剩余: {state_summary['queue_size']} 个
- 已爬取页面: {state_summary['crawled_pages']}/{state_summary['max_pages']}
- 成功: {state_summary['success']}, 失败: {state_summary['failed']}
- 已提取文章: {state_summary['extracted_articles']} 篇
- 最近错误: {json.dumps(state_summary['recent_errors'], ensure_ascii=False)[:200]}

请输出 JSON 决策（action: scrape/finish）:"""

    supervisor_messages = list(state.get("supervisor_messages", []))
    if not supervisor_messages:
        supervisor_messages = [
            SystemMessage(content=_SUPERVISOR_SYSTEM_PROMPT),
            HumanMessage(content=f"用户请求: 爬取 {base_url}。请根据当前状态输出 JSON 决策。"),
        ]
    supervisor_messages.append(HumanMessage(content=prompt))

    # ★ 熔断检查：连续非格式错（429/5xx/网络超时）≥ 3 次才终止
    # 400 (invalid_request_error) 是消息格式问题，不计入熔断计数
    consecutive_errors = 0
    for e in reversed(error_log):
        err_type = e.get("error_type", "")
        err_msg = e.get("message", "")
        # 400 格式错不计入熔断（应修消息后重试）
        if err_type == "llm_api_error" and (
            "invalid_request_error" in err_msg
            or ("400" in err_msg and "bad request" in err_msg.lower())
        ):
            continue  # 跳过，不打断计数链
        if err_type == "llm_api_error":
            consecutive_errors += 1
        else:
            break
    if consecutive_errors >= 3:
        agent_logger.warning(f"[Supervisor] 连续非格式错已达 {consecutive_errors} 次，触发熔断，强制终止")
        log_agent_thought("Supervisor", "error",
                          f"熔断: 连续 {consecutive_errors} 次 LLM API 错误（不含400格式错），请检查 API Key / 模型名称 / 网络连接")
        return {
            "supervisor_messages": list(supervisor_messages),
            "next_worker": "FINISH",
            "worker_data": {},
            "worker_results": {
                "decision": {"action": "finish",
"reasoning": f"熔断: 连续 {consecutive_errors} 次 LLM API 错误",
                             "priority_url": ""},
                "queue_size": queue_size,
                "crawled": crawled,
            },
        }

    # 调用 LLM
    print(f"[Supervisor] ⏳ 正在调用 LLM ({config.DEEPSEEK_MODEL}) 分析爬取状态...")
    agent_logger.info(f"[Supervisor] ⏳ 调用 LLM: model={config.DEEPSEEK_MODEL}, base_url={config.DEEPSEEK_BASE_URL}, key_len={len(config.DEEPSEEK_API_KEY)}")
    decision = {"action": "finish", "reasoning": "规则兜底", "priority_url": ""}
    try:
        llm = _get_supervisor_llm()
        response = llm.invoke(supervisor_messages)
        print(f"[Supervisor] ✅ LLM 响应完成")
        content = response.content if hasattr(response, "content") else str(response)
        decision = _parse_supervisor_json(content)
        agent_logger.info(f"[Supervisor] LLM 决策: {json.dumps(decision, ensure_ascii=False)}")
    except Exception as e:
        err_str = str(e)
        is_format_error = "invalid_request_error" in err_str or ("400" in err_str and "bad request" in err_str.lower())

        if is_format_error:
            # ★ 400 格式错不计入 error_log，降级到规则决策（消息历史可能脏了，但不应熔断）
            agent_logger.warning(f"[Supervisor] LLM 返回 400 格式错（不计入熔断），降级为规则决策: {e}")
            print(f"[DIAG] supervisor 400 格式错 → 降级规则决策 | {err_str[:150]}")
        else:
            agent_logger.warning(f"[Supervisor] LLM 调用失败（降级为规则决策）: {e}")
            error_log.append({
                "error_type": "llm_api_error",
                "url": base_url,
                "message": f"Supervisor LLM 调用失败: {err_str[:200]}",
                "timestamp": "",
            })
        # 降级规则
        if queue_size > 0 and crawled < max_pages - 1 and len(recent_errors) < 5:
            decision = {"action": "scrape", "reasoning": f"降级规则: 队列{queue_size}个, 继续爬取", "priority_url": ""}
        else:
            decision = {"action": "finish", "reasoning": "降级规则: 队列空或达到上限", "priority_url": ""}

    # 可观测性日志
    log_agent_thought("Supervisor", "thought",
                      f"{decision['reasoning']} | 队列:{queue_size} 已爬:{crawled}/{max_pages}")
    log_agent_thought("Supervisor", "action",
                      f"action={decision['action']} priority_url={decision['priority_url'][:80]}")

    # 构建 worker_data：从 url_queue 弹出下一个待处理 URL
    worker_data = {}
    next_url = base_url
    if decision["action"] in ("scrape", "retry"):
        # 优先使用 priority_url（LLM 推荐），否则从 url_queue 弹出第一个
        priority = decision.get("priority_url", "").strip()
        if priority and priority.startswith("http"):
            next_url = priority
            # 从队列中移除该 URL（如果存在）
            url_queue = [q for q in url_queue if q.get("url", "") != priority]
        elif url_queue:
            popped = url_queue.pop(0)
            next_url = popped.get("url", base_url)
            # ★ 传递完整的 queue item 信息（深度、面包屑等），供 post_worker_node 深度继承使用
            worker_data = {
                "action": "scrape",
                "url": next_url,
                "depth": popped.get("depth", 1),
                "nav_depth": popped.get("nav_depth", 1),
                "breadcrumb": popped.get("breadcrumb", []),
            }
        else:
            # 队列为空，用 base_url（首次进入或所有 URL 已处理完）
            next_url = base_url
            worker_data = {"action": "scrape", "url": next_url}
        
        # ★ 如果 worker_data 还没赋值（priority_url 分支），设置默认 depth
        if not worker_data:
            worker_data = {"action": "scrape", "url": next_url}
        agent_logger.info(f"[Supervisor] 分配 URL: {next_url[:80]}")
    # decision == "finish" 时 worker_data 保持空

    # 更新状态
    next_worker = "web_scraper" if decision["action"] in ("scrape", "retry") else "FINISH"

    return {
        "supervisor_messages": list(supervisor_messages),
        "next_worker": next_worker,
        "worker_data": worker_data,
        "worker_results": {
            "decision": decision,
            "queue_size": queue_size,
            "crawled": crawled,
        },
        "error_log": error_log,
    }
