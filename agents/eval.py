"""agents/eval.py — Agent 系统评估：规则指标 + LLM-as-judge

大厂叙事（离线评估三件套）：
  1. **指标可量化**：P/R/F1 是"召回型任务"（爬虫要尽量全、尽量准）的通用指标，
     让每次改动有可对比的数字，而非"感觉变好了"。
  2. **规则覆盖不了的维度交给 LLM-as-judge**：输出相关性、格式规范、指令遵循，
     judge 输出强制 JSON `{"score": 0-5, "reason": "..."}`，可解释、可审计。
  3. **回归对比**：两次运行产出的指标 diff → CI 或面试演示直接给"这次改动
     让 recall +0.2 / 成本 -15%"的结论。

judge 可插拔：传入任意 `judge_fn(system_prompt, user_content) -> str`，
线上接 ChatOpenAI/TrackedLLM，测试接 Fake。不绑定具体客户端。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Callable, Dict, Optional, Sequence, Set

# ── 1. 规则指标 ──


def compute_prf(expected: int, actual: int, overlap: int) -> Dict[str, float]:
    """基于集合口径的 P/R/F1。

    - expected: 期望命中数（golden 标注）
    - actual:   系统实际产出数
    - overlap:  实际产出 ∩ 期望（正确命中）
    """
    precision = overlap / actual if actual else 0.0
    recall = overlap / expected if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def heuristic_score(text: str, criteria: str = "") -> Dict[str, Any]:
    """确定性质量打分（`quality_judge` 工具的执行器，供 FC 评估裁决取"客观事实"）。

    信号：正文长度 / 链接密度 / 图片完整性，返回 0~1 分 + 理由列表。
    与模型无关、可离线测试——LLM 在 FC 循环里调用它拿到客观分后再做裁决，
    避免"LLM 凭感觉打分"无法复核。
    """
    t = (text or "").strip()
    text_len = len(t)
    links = len(re.findall(r"<a\b", t, re.I))
    imgs = len(re.findall(r"<img\b", t, re.I))

    score = 0.0
    reasons: list = []
    if text_len >= 1500:
        score += 0.5
        reasons.append("正文充足")
    elif text_len >= 300:
        score += 0.3
        reasons.append("正文一般")
    else:
        score += 0.1
        reasons.append("正文过短")
    if 1 <= links <= 20:
        score += 0.2
        reasons.append("链接密度正常")
    elif links == 0:
        score += 0.1
        reasons.append("无链接")
    else:
        reasons.append("链接过多")
    if imgs >= 2:
        score += 0.2
        reasons.append("图片完整")
    elif imgs == 1:
        score += 0.1
    else:
        reasons.append("无图片")
    return {"score": round(min(1.0, score), 2), "reasons": reasons, "text_len": text_len}


# ── 1.5 RAG 检索质量指标 ──


def recall_at_k(ranked_ids: Sequence[int], relevant_ids: Set[int], k: int) -> float:
    """检索质量：前 k 个结果中相关文档占比（相关文档全集为分母）。

    - ranked_ids:   检索排序后的文档 id 列表
    - relevant_ids: 与查询相关的文档 id 集合（人工标注）
    - 无相关文档时定义为 0（不可评估 → 不虚高）
    """
    if not relevant_ids:
        return 0.0
    hit = len({d for d in ranked_ids[:k]} & relevant_ids)
    return hit / len(relevant_ids)


def ndcg_at_k(ranked_ids: Sequence[int], relevant_ids: Set[int], k: int) -> float:
    """检索质量：NDCG@k（位置感知，靠前的相关文档得分更高）。

    无相关文档时返回 0.0；k 超过列表长度按列表长度截断。
    """
    if not relevant_ids:
        return 0.0
    ranked = list(ranked_ids[:k])
    rel = [1 if d in relevant_ids else 0 for d in ranked]
    dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rel))
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant_ids), k)))
    return round(dcg / ideal, 4) if ideal else 0.0


# ── 2. LLM-as-judge ──

_JUDGE_SYSTEM = (
    "你是评测裁判。你的任务：依据评分标准对给定的 Agent 输出打分。\n"
    "评分标准：\n{criteria}\n"
    '输出必须是严格 JSON：{{"score": 0 到 5 的整数, "reason": "一句话理由"}}，不要输出其它内容。'
)


def parse_judge_json(text: str) -> Optional[Dict]:
    """容错解析 judge 输出：剥离 markdown 围栏后尝试 json.loads。

    失败返回 None（调用方可重试或判 0 分，不抛异常——评测不能因为格式崩掉）。
    """
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 退化：直接从字符串里抠出第一个 JSON 对象
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", cleaned):
        try:
            obj, _ = decoder.raw_decode(cleaned[m.start():])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def llm_judge(
    judge_fn: Callable[[str, str], str],
    sample: str,
    criteria: str,
    max_retries: int = 1,
) -> Dict:
    """调用 judge_fn(system, user) 对样本打分。

    返回 `{"score": int, "reason": str, "parsed": bool}`；
    解析失败则重试（默认 1 次），仍失败返回 score=0 + parsed=False。
    """
    system = _JUDGE_SYSTEM.format(criteria=criteria)
    user = f"——Agent 输出样本——\n{sample}"
    for _ in range(max_retries + 1):
        try:
            raw = judge_fn(system, user)
        except Exception:
            raw = ""
        parsed = parse_judge_json(raw)
        if parsed is not None and "score" in parsed:
            try:
                score = int(parsed["score"])
            except (TypeError, ValueError):
                score = 0
            score = max(0, min(5, score))
            return {
                "score": score,
                "reason": str(parsed.get("reason", "")),
                "parsed": True,
            }
    return {"score": 0, "reason": "judge 输出无法解析", "parsed": False}
