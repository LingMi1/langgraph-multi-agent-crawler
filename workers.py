"""
Worker ReAct 节点 — 真正的 LLM 驱动循环 (Phase 3 重构)

职责:
  1. Worker Agent: LLM 分析状态 + 工具列表，输出 Thought → Action
  2. Worker Tools: 执行工具调用，返回结构化 Observation（含自纠错反馈）
  3. 循环控制: max_iterations=5 防止死循环
  4. 可观测性: 每一步输出 🤔/⚡/👁 日志
"""
import json
import os
from typing import Dict, Any, List, Union
from langgraph.graph import END
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage, BaseMessage
import config
from state import AgentState, agent_logger, log_agent_thought, log_section
import tools as agent_tools

_WORKER_LLM = None


# ======================================================================
# ★ sanitize_messages: 修复不合法的 messages 列表
# ======================================================================

def sanitize_messages(messages: List[BaseMessage]) -> List[BaseMessage]:
    """
    校验并修复 messages 列表，确保每条带 tool_calls 的 assistant 消息
    后面都紧跟对应的 role="tool" 结果消息。

    遍历 messages，遇到 tool_calls 未配对的，就补一条占位 ToolMessage。

    Returns:
        修复后的 messages 列表（若无需修复则返回原列表）
    """
    repaired: List[BaseMessage] = []
    patched_count = 0

    for i, msg in enumerate(messages):
        repaired.append(msg)

        # 检查当前消息是否带 tool_calls
        has_tc = hasattr(msg, "tool_calls") and msg.tool_calls

        if has_tc:
            declared_ids = []
            for tc in msg.tool_calls:
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                if tc_id:
                    declared_ids.append(tc_id)

            if not declared_ids:
                continue

            # 向后扫描，检查后续 ToolMessage 覆盖了哪些 tool_call_id
            # ★ 修复: 不再在遇到下一个带 tool_calls 的 AIMessage 时提前 break。
            #         改为扫描到消息列表末尾，因为 tool_call_id 全局唯一，
            #         不存在跨 batch 误匹配的风险。
            covered_ids = set()
            for j in range(i + 1, len(messages)):
                nxt = messages[j]
                nxt_id = getattr(nxt, "tool_call_id", None)
                if nxt_id:
                    covered_ids.add(nxt_id)

            # 补上声明的但未有对应 ToolMessage 的 tool_call_id
            for missing_id in declared_ids:
                if missing_id not in covered_ids:
                    placeholder = ToolMessage(
                        content="工具执行失败，无返回结果",
                        tool_call_id=missing_id,
                    )
                    repaired.append(placeholder)
                    patched_count += 1

    if patched_count > 0:
        agent_logger.warning(f"[sanitize_messages] 发现 {patched_count} 个缺失的 tool_call_id，已补全占位 ToolMessage")

    return repaired


def reset_worker_llm():
    """强制重置 Worker LLM 实例，使下次调用使用最新的 config 值"""
    global _WORKER_LLM
    _WORKER_LLM = None


# ReAct 最大循环次数
MAX_REACT_ITERATIONS = 5

# Worker 工具列表
WORKER_TOOLS = [
    agent_tools.fetch_page,
    agent_tools.extract_links,
    agent_tools.clean_and_extract,
    agent_tools.save_data,
    agent_tools.finish_task,
]


def _get_worker_llm():
    """Worker 专用 LLM（bind_tools 用于 ReAct 工具调用）"""
    global _WORKER_LLM
    if _WORKER_LLM is None:
        import httpx
        _WORKER_LLM = ChatOpenAI(
            model=config.get_model_name(),
            openai_api_key=config.DEEPSEEK_API_KEY,
            openai_api_base=config.DEEPSEEK_BASE_URL,
            temperature=0,
            max_tokens=4096,
            request_timeout=120,
            http_client=httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)),
        ).bind_tools(WORKER_TOOLS)
    return _WORKER_LLM


# ======================================================================
# System Prompt (增强版)
# ======================================================================

_WORKER_SYSTEM_PROMPT = """你是一个企业级网页爬取 AI Agent（web_scraper Worker），具备自主纠错能力。

## 可用工具
1. **fetch_page(url)** — 抓取页面 HTML。返回 {"success": bool, "html_length": int, "title": str, "http_status": int}
2. **extract_links(url, html)** — 提取页面同域链接。返回 {"total_found": int, "new_links": [...]}
3. **clean_and_extract(url, html)** — 清洗 HTML + 提取结构化数据。返回 {"success": bool, "article": {...}}
4. **save_data(data_json)** — 保存数据到 CSV。返回 {"saved_count": int, "csv_path": str}
5. **finish_task(summary)** — 结束任务返回 Supervisor

## 标准工作流程（必须严格执行！）
1. fetch_page(target_url) → 获取 HTML
2. **必须调用 extract_links(url, html)** → 发现新链接（系统会自动将新链接加入爬取队列）
3. clean_and_extract(url, html) → 清洗提取
4. save_data(articles_json) → 保存
5. 调用 finish_task 返回

## ★ 关键规则（必须遵守！）
- **每次处理一个页面都必须调用 extract_links**，即使你不关心新链接，系统需要它来发现更多页面
- 如果 fetch_page 返回 success=false:
  → 直接调用 finish_task 报告失败，不要继续后续步骤
- 如果 fetch_page 返回 success=true 但 html_length 很小（<500）:
  → 直接调用 finish_task 报告 "页面内容过少"，不要调用 extract_links
- 如果 clean_and_extract 返回 success=false:
  → 仍然调用 finish_task，但 summary 需说明原因
- 如果 fetch_page 返回 403/Cloudflare/WAF:
  → 直接 finish_task 报告 "站点反爬拦截"
- 如果 fetch_page 返回 timeout:
  → 可重试 1 次同一 URL，如果仍失败则 finish_task
- 遇到致命错误（连续失败 3 次），立即 finish_task

## 重要规则
- 每次只调用一个工具
- 优先处理 Supervisor 指定的 URL
- 如果所有 URL 都处理完毕，调用 finish_task
"""


# ======================================================================
# Worker Agent Node
# ======================================================================

def worker_agent_node(state: dict) -> Dict[str, Any]:
    """
    Worker 的 LLM 决策节点。
    注入当前状态摘要，让 LLM 做出知情决策。
    """
    log_section("Worker Agent: ReAct 循环")

    messages = list(state.get("messages", []))
    worker_data = state.get("worker_data", {})
    iteration = state.get("react_iteration", 0)
    # ★ Bug 2 修复: 如果 worker_data 中有新的 priority_url 或 url（说明 Supervisor 新派发了任务），
    #    则必须重置 ReAct 计数器，防止跨轮继承上一轮的满值导致开局即 finish
    new_task_url = worker_data.get("priority_url", worker_data.get("url", ""))
    if new_task_url and iteration > 0:
        # 检查是否是新一轮任务（messages 中无此 URL 的 fetch 记录）
        has_fetch_for_url = False
        for msg in messages:
            content_str = msg.content if hasattr(msg, "content") else str(msg)
            if new_task_url in content_str:
                has_fetch_for_url = True
                break
        if not has_fetch_for_url:
            agent_logger.info(f"[Worker Agent] 检测到新任务 URL={new_task_url[:60]}，重置 ReAct 计数器 (原值={iteration})")
            iteration = 0

    # 循环上限拦截
    if iteration >= MAX_REACT_ITERATIONS:
        log_agent_thought("Worker", "error", f"ReAct 循环已达上限 {MAX_REACT_ITERATIONS} 次，强制 finish_task")
        # ★ 先修复可能存在的残缺 tool_calls，再追加 force_finish 消息
        fixed = sanitize_messages(messages)
        fixed.append(AIMessage(content=f"force_finish: 超过最大循环 {MAX_REACT_ITERATIONS} 次"))
        return {
            "messages": fixed,
            "react_iteration": 0,
        }

    if not messages or iteration == 0:
        task_url = worker_data.get("priority_url", worker_data.get("url", state.get("root_url", "")))
        agent_logger.info(f"[Worker Agent] 新任务开始 | url={task_url[:60]} | iteration={iteration} | msg_count={len(messages)}")
        messages = [
            SystemMessage(content=_WORKER_SYSTEM_PROMPT),
            HumanMessage(content=f"Supervisor 分配任务: 爬取 {task_url} 并提取数据。请按标准流程执行。"),
        ]

    # ★ 调 LLM 前先校验/修复 messages
    messages = sanitize_messages(messages)

    llm = _get_worker_llm()
    try:
        response = llm.invoke(messages)
    except Exception as e:
        err_str = str(e)
        is_format_error = "invalid_request_error" in err_str or ("400" in err_str and "bad request" in err_str.lower())

        if is_format_error:
            # ★ 400 格式错：修复消息后重试一次，不计入熔断
            agent_logger.warning(f"[Worker Agent] LLM 返回 400 格式错 (invalid_request_error)，sanitize后重试...")
            print(f"[DIAG] worker_agent 400 格式错 → sanitize 后重试 | 原因: {err_str[:150]}")
            fixed_for_retry = sanitize_messages(messages)
            try:
                response = llm.invoke(fixed_for_retry)
                agent_logger.info(f"[Worker Agent] 400 重试成功")
            except Exception as retry_err:
                # 重试也失败 → 回退到原错误处理
                agent_logger.error(f"[Worker Agent] 400 重试也失败: {retry_err}")
                e = retry_err
                is_format_error = False  # 摔回通用错误处理
            else:
                # 重试成功，跳过通用错误处理
                pass

        if not is_format_error:
            # 非格式错（429/5xx/网络超时）：正常记录并计数熔断
            agent_logger.error(f"[Worker Agent] LLM 调用失败: {e}")
            error_log = list(state.get("error_log", []))
            error_log.append({
                "error_type": "llm_api_error",
                "url": state.get("root_url", ""),
                "message": f"Worker LLM 调用失败: {str(e)[:200]}",
                "timestamp": "",
            })
            fixed = sanitize_messages(messages)
            fixed.append(AIMessage(content=f"llm_error: {str(e)[:200]}"))
            return {
                "messages": fixed,
                "react_iteration": 0,
                "error_log": error_log,
            }

    # Token 计数
    usage = state.get("token_usage", {"prompt_tokens": 0, "completion_tokens": 0, "model": ""})
    try:
        if hasattr(response, "usage_metadata"):
            um = response.usage_metadata
            usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + um.get("input_tokens", 0)
            usage["completion_tokens"] = usage.get("completion_tokens", 0) + um.get("output_tokens", 0)
            usage["model"] = config.DEEPSEEK_MODEL
    except Exception:
        pass

    # 解析 LLM 思考过程
    content = response.content if hasattr(response, "content") else ""
    tool_calls = response.tool_calls if hasattr(response, "tool_calls") else []

    if content and content.strip():
        log_agent_thought("Worker", "thought", content[:300])

    for tc in tool_calls:
        tc_name = tc.get("name", "") if isinstance(tc, dict) else tc.name
        tc_args = tc.get("args", {}) if isinstance(tc, dict) else tc.args
        log_agent_thought("Worker", "action", f"调用工具: {tc_name}({json.dumps(tc_args, ensure_ascii=False)[:120]})")

    agent_logger.info(f"[Worker Agent] ReAct #{iteration+1}/{MAX_REACT_ITERATIONS} | tool_calls={len(tool_calls)}")

    return {
        "messages": [response],
        "token_usage": usage,
        "react_iteration": iteration + 1,
    }


# ======================================================================
# Worker Tools Node
# ======================================================================

def worker_tools_node(state: dict) -> Dict[str, Any]:
    """
    Worker 的工具执行节点，含自纠错逻辑。
    
    工具执行失败时不退出，而是构造结构化错误信息注入消息历史，
    让 LLM 在下一轮 ReAct 中自主决定应对策略。
    """
    messages = list(state.get("messages", []))
    if not messages:
        return {}

    last_msg = messages[-1]
    tool_calls = []
    if hasattr(last_msg, "tool_calls"):
        tool_calls = last_msg.tool_calls

    if not tool_calls:
        return {}

    tool_map = {t.name: t for t in WORKER_TOOLS}
    extracted_articles = list(state.get("extracted_data", []))
    tool_messages: List[ToolMessage] = []

    for tc in tool_calls:
        tool_name = tc.get("name") if isinstance(tc, dict) else tc.name
        tool_args = tc.get("args", {}) if isinstance(tc, dict) else tc.args
        tool_id = tc.get("id", "") if isinstance(tc, dict) else tc.id

        agent_logger.info(f"[Worker Tools] 执行: {tool_name}")
        log_agent_thought("Worker", "observation", f"执行 {tool_name} ...")

        if tool_name not in tool_map:
            result = json.dumps({"success": False, "error": f"未知工具: {tool_name}", "suggestion": "检查工具名拼写，重试"}, ensure_ascii=False)
        else:
            try:
                tool_fn = tool_map[tool_name]
                if tool_name == "save_data":
                    if isinstance(tool_args, dict) and "articles" not in tool_args and extracted_articles:
                        tool_args["articles"] = extracted_articles
                    result = tool_fn.invoke(tool_args)
                elif tool_name == "clean_and_extract":
                    result_str = tool_fn.invoke(tool_args)
                    try:
                        parsed = json.loads(result_str)
                        if parsed.get("success") and parsed.get("article"):
                            extracted_articles.append(parsed["article"])
                    except (json.JSONDecodeError, KeyError):
                        pass
                    result = result_str
                elif tool_name == "finish_task":
                    result = tool_fn.invoke(tool_args)
                    try:
                        parsed = json.loads(result)
                        log_agent_thought("Worker", "observation",
                                          f"finish_task: {parsed.get('summary', '')[:150]}")
                    except json.JSONDecodeError:
                        pass
                else:
                    result = tool_fn.invoke(tool_args)

                # 自动解析结果并增强 Observation
                try:
                    parsed_result = json.loads(result)
                    if isinstance(parsed_result, dict):
                        if not parsed_result.get("success", True) and parsed_result.get("error"):
                            error_info = parsed_result["error"]
                            suggestion = ""
                            if "403" in str(error_info) or "Cloudflare" in str(error_info) or "WAF" in str(error_info):
                                suggestion = "网站有反爬机制，跳过此 URL"
                            elif "timeout" in str(error_info).lower():
                                suggestion = "网络超时，换个时间重试"
                            elif "visited" in str(error_info).lower():
                                suggestion = "URL 已爬取，无需重复处理"
                            else:
                                suggestion = "尝试下一 URL"
                            # 增强错误反馈
                            enhanced = {
                                **parsed_result,
                                "suggestion": suggestion,
                                "ecoachable": True,
                            }
                            result = json.dumps(enhanced, ensure_ascii=False)
                            log_agent_thought("Worker", "error",
                                              f"{tool_name}: {error_info[:150]} → {suggestion}")
                        elif parsed_result.get("success"):
                            # 成功时简要日志
                            summary_keys = [k for k in ["html_length", "total_found", "saved_count", "images_count"] if k in parsed_result]
                            if summary_keys:
                                summary = ", ".join(f"{k}={parsed_result[k]}" for k in summary_keys)
                                log_agent_thought("Worker", "observation", f"{tool_name} 完成: {summary}")
                            else:
                                log_agent_thought("Worker", "observation", f"{tool_name} 执行成功")
                except (json.JSONDecodeError, TypeError):
                    log_agent_thought("Worker", "observation", f"{tool_name}: {str(result)[:120]}")

            except Exception as e:
                error_result = {
                    "success": False,
                    "error": f"{type(e).__name__}: {str(e)[:200]}",
                    "suggestion": "工具执行异常，尝试下一个操作",
                    "ecoachable": True,
                }
                result = json.dumps(error_result, ensure_ascii=False)
                agent_logger.error(f"[Worker Tools] 失败: {tool_name} | {type(e).__name__}: {str(e)[:200]}")
                log_agent_thought("Worker", "error", f"{tool_name} 执行异常: {str(e)[:120]}")

        tool_messages.append(ToolMessage(content=result, tool_call_id=tool_id))

    return {
        "messages": tool_messages,
        "extracted_data": extracted_articles,
    }


# ======================================================================
# Worker 循环路由
# ======================================================================

def worker_should_continue(state: dict) -> str:
    """
    决定 Worker 是否继续 ReAct 循环。
    
    Returns:
      "worker_tools" — 继续执行工具
      END — 结束当前 Worker 调用，返回 Supervisor
    """
    messages = state.get("messages", [])
    iteration = state.get("react_iteration", 0)

    # 循环上限拦截
    if iteration >= MAX_REACT_ITERATIONS:
        agent_logger.info(f"[Worker Router] 达到最大循环次数 {MAX_REACT_ITERATIONS}，返回 Supervisor")
        return END

    if not messages:
        return END

    last_msg = messages[-1]

    # 如果最后一条消息有 tool_calls，继续执行工具
    if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
        return "worker_tools"

    # 检查是否包含 force_finish 信号
    if hasattr(last_msg, "content") and last_msg.content:
        if "force_finish" in str(last_msg.content) or "llm_error" in str(last_msg.content):
            agent_logger.info(f"[Worker Router] 检测到 force_finish/llm_error 信号，返回 Supervisor")
            return END

    # 如果是 finish_task 的结果，Worker 应结束回到 Supervisor
    # (通过检查 tool_messages 中是否有 finish_task)
    for msg in reversed(messages):
        if hasattr(msg, "name") and getattr(msg, "name", "") == "finish_task":
            return END

    return END
