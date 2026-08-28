# -*- coding: utf-8 -*-
"""
智能定向爬取管线（LLM 驱动）— 新引擎，与旧 BFS 全站遍历并存。

设计（已与用户 grill-me 确认）：
  1. 只抓导航栏 1-4 级栏目页 + 从列表栏目进入的文章详情页 / 图集图片
  2. 首页仅用于导航发现（两跳：首页 + 各一级栏目页），不输出内容
  3. 两段式清洗：LLM 只返回正文容器定位签名 + 元信息，
     content_html 由代码从原站 DOM 搬 outerHTML 生成（原 class/style/CSS 零损耗，无输出截断）
  4. 图片复用 graph.nodes._embed_images_in_html → base64 内嵌（防盗链全复用，失败图整块删除）
  5. 输出按提示词契约：ywlx1/2/3 目录树 + title 文件名 + 全字段 CSV + 卡片壳 HTML

5 个提示词文件全部启用：
  - 提取提示词.txt          → 导航发现（两跳，产出 1-4 级树，含 gsmc / list/pages 分类 / is_image_only）
  - 新闻列表页提示词.txt     → 列表栏目页提取文章详情链接
  - 图片列表页提示词.txt     → 图集栏目页收图
  - 清洗提示词.txt          → 内容页清洗（标题规则 / 噪音清单 / 输出 JSON 契约，由两段式实现）
  - 正文渲染代码.txt        → 卡片壳渲染契约（本项目用等价 Python 实现）
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

import config
from schemas import agent_logger

# ============================================================================
# 提示词加载（从项目根目录 .txt 读取，不改原文）
# ============================================================================

_PROMPT_DIR = Path(__file__).resolve().parent.parent

_PROMPT_CACHE: Dict[str, str] = {}


def _load_prompt(name: str) -> str:
    """按文件名加载提示词，带缓存"""
    if name not in _PROMPT_CACHE:
        path = _PROMPT_DIR / name
        _PROMPT_CACHE[name] = path.read_text(encoding="utf-8")
    return _PROMPT_CACHE[name]


def get_prompt(name: str) -> str:
    return _load_prompt(name)


# ============================================================================
# LLM 客户端（OpenAI 兼容 / DeepSeek），JSON 严格输出 + 重试 + 多 provider 故障转移
# ============================================================================

_llm_client = None
_llm_provider_idx = 0  # 当前 provider 下标（0 = 主 DEEPSEEK_*，1+ = 备用）


def _providers() -> List[Dict]:
    """主 provider + 备用 providers。备用 base_urls 以 | 分隔，key/model 可选覆盖。"""
    primary = {
        "base_url": config.DEEPSEEK_BASE_URL,
        "api_key": config.DEEPSEEK_API_KEY,
        "model": config.DEEPSEEK_MODEL,
    }
    backup_urls = [
        u.strip()
        for u in (getattr(config, "LLM_BACKUP_BASE_URLS", "") or "").split("|")
        if u.strip()
    ]
    backup_keys = [
        k.strip()
        for k in (getattr(config, "LLM_BACKUP_API_KEYS", "") or "").split("|")
        if k.strip()
    ]
    backup_models = [
        m.strip()
        for m in (getattr(config, "LLM_BACKUP_MODELS", "") or "").split("|")
        if m.strip()
    ]
    providers = [primary]
    for i, url in enumerate(backup_urls):
        providers.append(
            {
                "base_url": url,
                "api_key": backup_keys[i] if i < len(backup_keys) else primary["api_key"],
                "model": backup_models[i] if i < len(backup_models) else primary["model"],
            }
        )
    return providers


def _provider_count() -> int:
    return len(_providers())


def _current_model() -> str:
    """当前 provider 的模型名（全小写，DeepSeek 系 API 要求）"""
    return (_providers()[_llm_provider_idx]["model"] or "deepseek-chat").lower()


def _get_llm():
    """懒加载当前 provider 的 OpenAI 客户端（DeepSeek 兼容接口）"""
    global _llm_client
    if _llm_client is None:
        p = _providers()[_llm_provider_idx]
        from openai import AsyncOpenAI
        _llm_client = AsyncOpenAI(
            api_key=p["api_key"],
            base_url=p["base_url"],
            timeout=600,  # v4-flash 推理大 DOM（40000 字符）可能超 120s
        )
    return _llm_client


def _switch_provider() -> bool:
    """切换到下一个备用 provider（重建客户端）；没有更多 provider 返回 False"""
    global _llm_client, _llm_provider_idx
    if _llm_provider_idx + 1 >= _provider_count():
        return False
    _llm_provider_idx += 1
    _llm_client = None
    return True


def reset_llm():
    global _llm_client, _llm_provider_idx
    _llm_client = None
    _llm_provider_idx = 0


async def chat_json(
    system_prompt: str,
    user_content: str,
    temperature: float = 0.0,
    max_tokens: int = 32768,
    retries: int = 2,
) -> Optional[Dict]:
    """
    调用 LLM 并要求返回 JSON。返回解析后的 dict；全部重试失败返回 None。
    从响应中容错提取 JSON（去 ```json 围栏、取首个 { ... } 平衡块）。

    ★ 多 provider 故障转移：当前 provider 重试耗尽自动切换到备用
      （LLM_BACKUP_BASE_URLS，| 分隔），全部 provider 失败才记 1 次连续失败。

    ★ 运行级熔断（agents/breaker.py）：熔断打开时直接返回 None（零等待），
      调用方按"无 LLM"降级——llm_locate 回退代码启发式、导航分类回退规则。
      一次调用成功 → 计数清零；全部 provider 重试耗尽 → 记 1 次连续失败。
    """
    from agents.breaker import llm_breaker

    if not llm_breaker.check():
        return None
    if _get_llm() is None:
        agent_logger.error("[LLM] 客户端未初始化（检查 DEEPSEEK_API_KEY）")
        return None
    last_err = ""
    provider_attempts = 0
    total_providers = _provider_count()
    while provider_attempts < total_providers:
        client = _get_llm()
        for attempt in range(retries + 1):
            try:
                resp = await client.chat.completions.create(
                    model=_current_model(),
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                text = resp.choices[0].message.content or ""
                if not text:
                    agent_logger.warning(f"[LLM] 返回空 content（finish={resp.choices[0].finish_reason}），可能是 reasoning 模型 token 耗尽")
                parsed = _parse_json(text)
                if parsed is not None:
                    llm_breaker.record_success()
                    return parsed
                last_err = "JSON 解析失败"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            agent_logger.warning(f"[LLM] 调用失败(重试{attempt + 1}/{retries + 1}): {last_err}")
            if attempt < retries:
                await asyncio.sleep(2.0 * (attempt + 1))
        # 当前 provider 重试耗尽 → 切换备用 provider
        if not _switch_provider():
            break
        provider_attempts += 1
        agent_logger.warning(f"[LLM] 当前 provider 不可用，切换备用（{provider_attempts + 1}/{total_providers}）")
    llm_breaker.record_failure(last_err)
    return None


async def chat_stream(
    system_prompt: str,
    user_content: str,
    temperature: float = 0.0,
    max_tokens: int = 32768,
):
    """
    流式调用 LLM，逐块产出文本片段（async generator）。

    与 chat_json 共用基础设施：熔断检查 + 多 provider 故障转移
    （create 阶段失败才切换；流中途异常直接终止，不重复浪费）。
    调用方按"已收到的片段"拼装即可；失败/熔断时产出为空。
    """
    from agents.breaker import llm_breaker

    if not llm_breaker.check():
        return
    if _get_llm() is None:
        agent_logger.error("[LLM] 客户端未初始化（检查 DEEPSEEK_API_KEY）")
        return
    last_err = ""
    provider_attempts = 0
    total_providers = _provider_count()
    while provider_attempts < total_providers:
        client = _get_llm()
        try:
            stream = await client.chat.completions.create(
                model=_current_model(),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            agent_logger.warning(f"[LLM] 流式调用失败: {last_err}")
            if not _switch_provider():
                break
            provider_attempts += 1
            continue
        # 流建立成功：逐块产出；中途异常直接终止（不切换 provider）
        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                piece = getattr(chunk.choices[0].delta, "content", None) or ""
                if piece:
                    yield piece
        except Exception as e:
            agent_logger.warning(f"[LLM] 流中途异常，提前终止: {type(e).__name__}: {e}")
            llm_breaker.record_failure(f"stream abort: {type(e).__name__}")
            return
        llm_breaker.record_success()
        return
    llm_breaker.record_failure(last_err)


def _parse_json(text: str) -> Optional[Dict]:
    """容错解析 LLM 输出的 JSON：去围栏 → 提取首个平衡花括号块 → json.loads"""
    if not text:
        return None
    t = text.strip()
    # 去 ```json ... ``` 围栏
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    # 找首个 { 与对应的平衡 }（跳过字符串里的花括号）
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    end = -1
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return None
    try:
        return json.loads(t[start:end + 1])
    except Exception:
        return None


# ============================================================================
# 正文压缩（两段式第一步：喂给 LLM 的精简 DOM，去 script/base64/长文本）
# ============================================================================

_MAX_ATTR_TEXT = 50  # 文本节点截断字数
_MAX_CHILDREN = 25  # 每节点最多保留的子元素数


def _compress_node(node, depth: int, buf: List[str], max_depth: int = 40):
    """递归压缩 DOM：输出 <tag#id.class href=... src=...> 签名 + 前 N 字文本，去 script/style/base64"""
    if depth > max_depth:
        return
    if node.name in ("script", "style", "noscript", "iframe", "svg", "template"):
        return
    attrs = getattr(node, "attrs", None) or {}
    cls = " ".join(attrs.get("class") or [])[:120]
    nid = attrs.get("id") or ""
    sig = node.name
    if nid:
        sig += f"#{nid}"
    if cls:
        sig += "." + cls.replace(" ", ".")
    # ★ 关键属性：a 的 href、img/picture 的 src，LLM 提取 URL 全靠它们
    if node.name == "a":
        href = (attrs.get("href") or "").strip()
        if href and not href.startswith(("#", "javascript", "mailto", "tel", "data:")):
            sig += f' href="{href[:200]}"'
    elif node.name == "img":
        src = (attrs.get("src") or attrs.get("data-src") or attrs.get("data-original") or "").strip()
        if src and not src.startswith("data:"):
            sig += f' src="{src[:200]}"'
    elif node.name in ("ul", "ol") and attrs.get("class"):
        # 列表容器保留 class（导航/图集识别用）
        pass
    buf.append("<" + sig + ">")
    # 直接文本子节点（不含后代，避免重复输出）
    try:
        direct_text = "".join(
            s for s in node.contents
            if isinstance(s, str) and s.strip()
        )
    except Exception:
        direct_text = ""
    if direct_text:
        buf.append(direct_text.strip()[:_MAX_ATTR_TEXT])
    if depth + 1 <= max_depth:
        for i, child in enumerate(node.children):
            if i >= _MAX_CHILDREN:
                buf.append("...")
                break
            if child.name:
                _compress_node(child, depth + 1, buf, max_depth)
    buf.append("</" + node.name + ">")


def compress_html(html: str, max_len: int = 40000) -> str:
    """压缩完整 HTML 为轻量 DOM 骨架（供 LLM 定位正文容器），超长截断"""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return html[:max_len]
    buf: List[str] = []
    for child in soup.find_all(["body"]) or [soup]:
        for c in child.children:
            if getattr(c, "name", None):
                _compress_node(c, 0, buf)
    out = "".join(buf)
    if len(out) > max_len:
        out = out[:max_len] + "\n...(截断)"
    return out


# ============================================================================
# 工具
# ============================================================================

def _safe_filename(title: str, max_len: int = 80) -> str:
    """标题 → 安全文件名（去除非法字符）"""
    t = re.sub(r'[\\/:*?"<>|\r\n\t]', "", title or "").strip()
    if not t:
        t = "untitled"
    return t[:max_len]


def _json_dumps_ensure_ascii(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _merge_nav_records(records: List[Dict]) -> List[Dict]:
    """同 URL 去重，保留 ywlx 路径最深的一条（提取提示词的同 URL 去重规则）"""
    best: Dict[str, Dict] = {}
    for r in records:
        if not r or not r.get("url"):
            continue
        u = r["url"]
        old = best.get(u)
        if old is None:
            best[u] = r
            continue
        # 比较层级深度
        def depth(x: Dict) -> int:
            return sum(1 for i in ("ywlx1", "ywlx2", "ywlx3", "ywlx4") if x.get(i))
        if depth(r) > depth(old):
            best[u] = r
    return list(best.values())


def _base_host(url: str) -> str:
    return urlparse(url).netloc
