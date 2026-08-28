"""agents/tools.py — Tool 层抽象（工具注册 / schema / 执行器 / 参数净化）

把爬虫管道的确定性能力封装为可注册、可调用、带 JSON Schema 的"工具集"，
供 Agent 调用（面试叙事：Agent 不是把逻辑写死，而是通过 ToolRegistry
暴露能力，为后续 Function Calling / MCP 化留口子）。

内置工具（全部包装项目真实函数，graph.nodes 惰性导入避免循环依赖）：
  - url_key                    URL 归一化去重键
  - md5                        内容哈希（去重）
  - is_pagination_url          分页链接识别
  - is_pure_image_product_detail  规则13 纯图产品详情页判定
  - detect_injection           提示注入弱检测（safety 层）
  - sanitize_text              不可信文本清理（safety 层）
  - jaccard_similarity         n-gram Jaccard 文本相似度（RAG 语义去重）
  - near_duplicate_pages       批量近重复检测（聚类同一栏目重复页）
  - quality_judge              确定性质量打分（FC 评估链路）

Tool-Use 安全第一层（sanitize_tool_args）：LLM 生成的工具参数是"半可信"输入
（可能被注入污染上下文影响），执行前按工具 JSON Schema 统一净化——剥离未知
字段、字符串截断、类型强制。ToolRegistry.call 内置净化，调用方零改动。

用法:
  tools = ToolRegistry.builtin()
  tools.call("url_key", url="http://a.com/p?id=1&utm_source=x")
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from agents.safety import detect_injection, sanitize_text

# 字符串参数净化默认上限（防超长/注入样本撑爆内存与日志）
_DEFAULT_MAX_STR_LEN = 4000


def sanitize_tool_args(
    name: str,
    args: Dict[str, Any],
    parameters: Dict[str, Any],
    max_str_len: int = _DEFAULT_MAX_STR_LEN,
) -> Dict[str, Any]:
    """按工具 JSON Schema 净化 LLM 传入的工具参数（Tool-Use 安全第一层）。

    风险模型：LLM 决策可能被注入污染的页面内容带偏，生成畸形/超长/未知参数。
    净化规则（幂等，不抛异常——净化是保险丝不是闸门）：
      1. 仅保留 schema 声明的字段（剥离未知 key，防意外副作用参数）
      2. 字符串截断到上限（防超长/注入样本）
      3. 类型强制：数值解析失败 / 非法类型即剥离
    """
    properties = (parameters or {}).get("properties", {}) or {}
    out: Dict[str, Any] = {}
    for key, prop in properties.items():
        if key not in args:
            continue
        val = args[key]
        ptype = prop.get("type", "")
        if ptype == "string":
            out[key] = str(val)[:max_str_len]
        elif ptype == "integer":
            try:
                out[key] = int(val)
            except (TypeError, ValueError):
                continue
        elif ptype == "number":
            try:
                out[key] = float(val)
            except (TypeError, ValueError):
                continue
        elif ptype == "boolean":
            out[key] = bool(val)
        elif ptype == "array":
            items = prop.get("items", {}) or {}
            if items.get("type") == "string":
                out[key] = [str(v)[:max_str_len]
                            for v in (val if isinstance(val, (list, tuple)) else [])]
            else:
                out[key] = val if isinstance(val, (list, tuple)) else []
        else:
            # 未知类型（项目内不会出现）→ 原样保留
            out[key] = val
    return out


class Tool:
    """一个可注册工具：名称 / 描述 / 参数 JSON Schema / 执行器。"""

    def __init__(self, name: str, description: str,
                 parameters: Dict[str, Any], executor: Callable[..., Any]):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.executor = executor

    def schema(self) -> Dict[str, Any]:
        """工具声明（Function Calling 的 tool schema 格式）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def __call__(self, **kwargs: Any) -> Any:
        return self.executor(**kwargs)


def quality_judge_tool() -> Tool:
    """`quality_judge` 工具：确定性质量打分（FC 评估路径与 builtin 共用）。"""
    from agents.eval import heuristic_score

    return Tool(
        "quality_judge", "对样本做确定性质量打分（正文长度/链接密度/图片，0~1 分+理由）",
        {
            "type": "object",
            "properties": {
                "sample": {"type": "string"},
                "criteria": {"type": "string", "default": ""},
            },
            "required": ["sample"],
        },
        lambda sample, criteria="": heuristic_score(sample, criteria),
    )


class ToolRegistry:
    """工具注册表：注册 / 查询 / 调用 / 导出 schema。"""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def call(self, name: str, **kwargs: Any) -> Any:
        """调用工具（执行前按 JSON Schema 净化参数）；未注册抛 KeyError。"""
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"未注册的工具: {name} (可用: {sorted(self._tools)})")
        kwargs = sanitize_tool_args(name, kwargs, tool.parameters)
        return tool(**kwargs)

    def all_schemas(self) -> list:
        return [t.schema() for t in self._tools.values()]

    def names(self) -> list:
        return sorted(self._tools)

    @staticmethod
    def builtin() -> "ToolRegistry":
        """注册内置工具集（包装项目真实确定性能力）。"""
        reg = ToolRegistry()

        def _exec_url_key(url: str) -> str:
            from graph.nodes import _url_key
            return _url_key(url)

        def _exec_md5(text: str) -> str:
            from agents.extractor import _compute_md5
            return _compute_md5(text or "")

        def _exec_pagination(url: str) -> bool:
            from graph.nodes import _is_pagination_url
            return _is_pagination_url(url)

        def _exec_pure_img_detail(html: str) -> bool:
            from graph.nodes import _is_pure_image_product_detail
            return _is_pure_image_product_detail(html or "")

        reg.register(Tool(
            "url_key", "URL 归一化去重键（过滤追踪参数 / 折叠 index.php 变体）",
            {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            _exec_url_key,
        ))
        reg.register(Tool(
            "md5", "计算内容 MD5（用于跨页去重）",
            {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            _exec_md5,
        ))
        reg.register(Tool(
            "is_pagination_url", "判断 URL 是否为分页链接（防 uuid-N 详情误判）",
            {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            _exec_pagination,
        ))
        reg.register(Tool(
            "is_pure_image_product_detail", "规则13：纯图产品详情页强信号判定",
            {"type": "object", "properties": {"html": {"type": "string"}}, "required": ["html"]},
            _exec_pure_img_detail,
        ))
        reg.register(Tool(
            "detect_injection", "弱检测不可信内容中的提示注入（返回命中片段或 None）",
            {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
            detect_injection,
        ))
        reg.register(Tool(
            "sanitize_text", "清理不可信文本（去控制字符 + 截断）",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "max_len": {"type": "integer", "default": 4000},
                },
                "required": ["text"],
            },
            sanitize_text,
        ))

        from agents.semdedup import jaccard_similarity, near_duplicate_pages

        reg.register(Tool(
            "jaccard_similarity", "n-gram Jaccard 文本相似度（近重复判定，0~1）",
            {
                "type": "object",
                "properties": {
                    "a": {"type": "string"},
                    "b": {"type": "string"},
                    "n": {"type": "integer", "default": 3},
                },
                "required": ["a", "b"],
            },
            jaccard_similarity,
        ))
        reg.register(Tool(
            "near_duplicate_pages", "批量近重复检测：[(i,j,score)] 相似度>=threshold 的页面对",
            {
                "type": "object",
                "properties": {
                    "texts": {"type": "array", "items": {"type": "string"}},
                    "threshold": {"type": "number", "default": 0.6},
                },
                "required": ["texts"],
            },
            near_duplicate_pages,
        ))

        reg.register(quality_judge_tool())
        return reg
