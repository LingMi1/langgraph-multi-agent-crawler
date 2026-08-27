"""
runner_base.py — 业务可替换的 TaskRunner 协议

桌面壳（desktop_app.py + webui/）只面向 TaskRunner 接口运行：
    run(inputs, log_cb, progress_cb, pause_event, stop_event) -> stats

换业务 = 实现一个 TaskRunner 子类 + 声明字段 schema，
前端表单会按 schema 自动重排 —— 这就是"爬虫代码换成其他业务代码也能跑"的架构支点。

内置两个业务：
    CrawlerRunner   —— AI Agent 爬虫（默认 dry_run 演示；dry_run=False 时调用真实 main.run_langgraph_crawler）
    SampleRunner    —— 示例业务「批量文本摘要」（完全不同的字段集，用于演示换业务零改动）
"""
import dataclasses
import threading
import time
from abc import ABC, abstractmethod
from typing import Callable, List, Optional

# 回调类型：日志 / 进度
LogCb = Callable[[str], None]
ProgressCb = Callable[[int, int, str, str], None]  # (done, queue_len, current, phase)


@dataclasses.dataclass
class FieldSpec:
    """表单字段声明：前端按此动态渲染。"""
    key: str                    # 字段名
    label: str                  # 显示标签
    type: str = "text"          # text / password / select / textarea / file / number
    placeholder: str = ""
    options: Optional[List[str]] = None   # select 用
    default: str = ""
    required: bool = False


class TaskRunner(ABC):
    """业务 Runner 抽象：壳不感知具体业务。"""

    name: str = "未命名业务"
    fields: List[FieldSpec] = []

    @abstractmethod
    def run(self, inputs: dict,
            log_cb: LogCb,
            progress_cb: ProgressCb,
            pause_event: threading.Event,
            stop_event: threading.Event) -> dict:
        """执行任务。

        Args:
            inputs:     按 self.fields 收集的表单值（key -> 字符串）
            log_cb:     日志回调
            progress_cb: 进度回调 (done, queue_len, current, phase)
            pause_event / stop_event: 暂停 / 结束 信号

        Returns:
            stats: {"total", "done", "failed", "queue", "current", "phase"} 通用统计
        """
        raise NotImplementedError

    # ---- 通用小工具 ----
    @staticmethod
    def parse_lines(text: str) -> List[str]:
        """按 空格/逗号/换行 拆分并去重。"""
        import re
        items = []
        for raw in re.split(r"[\s,，\n]+", text or ""):
            item = raw.strip()
            if item and item not in items:
                items.append(item)
        return items

    @staticmethod
    def read_url_file(path: str, limit: int = 30) -> List[str]:
        """读取 .txt 文件（每行一个，# 开头为注释），最多 limit 个。"""
        urls = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line not in urls:
                        urls.append(line)
                    if len(urls) >= limit:
                        break
        except Exception:
            pass
        return urls

    @staticmethod
    def wait_pause(pause_event: threading.Event) -> None:
        """暂停挂起（不打断 stop 语义：循环会在外层检查 stop）。"""
        while pause_event.is_set():
            time.sleep(0.3)


# ============================================================================
# 业务 1：AI Agent 爬虫
# ============================================================================

PLATFORMS = [
    ("DeepSeek（深度求索）", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("SiliconFlow（硅基流动）", "https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3"),
    ("Moonshot（月之暗面）", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    ("智谱清言（GLM）", "https://open.bigmodel.cn/api/paas/v4", "glm-4"),
    ("通义千问（阿里云）", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo"),
    ("OpenAI 官方", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("自定义", "", ""),
]


class CrawlerRunner(TaskRunner):
    name = "AI Agent 爬虫"

    fields = [
        FieldSpec("platform", "平台选择", "select",
                  options=[p[0] for p in PLATFORMS], default="DeepSeek（深度求索）"),
        FieldSpec("base_url", "API 地址", "text",
                  placeholder="https://api.deepseek.com/v1"),
        FieldSpec("api_key", "OpenAI Key", "password", placeholder="sk-..."),
        FieldSpec("model_name", "模型名称", "text", placeholder="deepseek-chat"),
        FieldSpec("file_path", "导入网址文件（.txt，每行一个）", "file",
                  placeholder="未选择文件"),
        FieldSpec("urls", "或直接输入网址（空格/逗号/换行分隔）", "textarea",
                  placeholder="https://example.com\nhttps://example2.com"),
        FieldSpec("concurrency", "并发数", "number", default="10"),
    ]

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run  # True=演示（模拟）；False=真爬（调 main.run_langgraph_crawler）

    def _collect_urls(self, inputs: dict) -> List[str]:
        urls = self.parse_lines(inputs.get("urls"))
        if not urls and inputs.get("file_path"):
            urls = self.read_url_file(inputs["file_path"])
        return urls

    def run(self, inputs, log_cb, progress_cb, pause_event, stop_event):
        urls = self._collect_urls(inputs)
        if not urls:
            # 演示占位：真实跑过的站点（详见 reports/campus_report.md），避免 example.com 假数据
            # ★ hnbn666 只支持 http（https 会被服务器直接断连），其余站点 https 可达
            urls = [
                "http://www.hnbn666.cn/",
                "https://www.xnjzgc.cn/",
                "https://www.zsyllh.cn/",
                "https://www.zztzmjg.com/",
                "https://www.cqht.cn/",
            ]
        total = len(urls)
        done = failed = 0
        phase = "等待"
        log_cb("已收到爬虫配置：base_url=%s | model=%s | 站点数=%d"
               % (inputs.get("base_url"), inputs.get("model_name"), total))
        if self.dry_run:
            log_cb("【演示模式】dry_run=True，仅模拟流程，不真正爬取（改 True 可接真实爬虫）")
        else:
            log_cb("【真实模式】将逐站调用 main.run_langgraph_crawler")

        for i, u in enumerate(urls, 1):
            if stop_event.is_set():
                log_cb("已结束任务，跳过剩余站点")
                break
            self.wait_pause(pause_event)
            phase = "fetch"
            progress_cb(done, total - done, u, phase)
            if self.dry_run:
                time.sleep(0.8)
                log_cb("模拟抓取 %s" % u[:60])
                done += 1
            else:
                # 真实爬取：复用 langgraph 工作流
                try:
                    import main as langgraph_main
                    n = langgraph_main.run_langgraph_crawler(
                        u,
                        concurrency=int(inputs.get("concurrency") or 10),
                        log_callback=log_cb,
                        progress_callback=progress_cb,
                    )
                    done += n if n > 0 else 0
                    failed += 1 if n == 0 else 0
                except Exception as e:
                    log_cb("站点 %s 爬取异常: %s" % (u[:60], e))
                    failed += 1
            progress_cb(done, total - done, u, phase)

        if stop_event.is_set():
            log_cb("任务被用户结束，已完成 %d/%d" % (done, total))
        else:
            log_cb("任务完成：成功 %d，失败 %d（共 %d）" % (done, failed, total))
        return {"total": total, "done": done, "failed": failed,
                "queue": total - done, "current": "", "phase": "完成"}


# ============================================================================
# 业务 2：示例业务 —— 批量文本摘要（完全不同的字段，证明换业务零改动）
# ============================================================================

class SampleRunner(TaskRunner):
    name = "批量文本摘要（示例业务）"

    fields = [
        FieldSpec("texts", "待处理文本（每行一段）", "textarea",
                  placeholder="今天天气很好……\n本项目采用 LangGraph 多 Agent 架构……"),
        FieldSpec("lang", "摘要语言", "select", options=["中文", "English", "日本語"]),
        FieldSpec("style", "摘要风格", "select", options=["简洁一句话", "要点列表", "学术严谨"]),
        FieldSpec("max_len", "最大字数", "number", default="120"),
        FieldSpec("out_path", "导出文件路径", "text", placeholder="output/summary.txt"),
    ]

    def run(self, inputs, log_cb, progress_cb, pause_event, stop_event):
        texts = [t.strip() for t in (inputs.get("texts") or "").splitlines() if t.strip()]
        if not texts:
            texts = ["示例文本 A：AI Agent 是当前最热的方向。",
                     "示例文本 B：本工具验证业务可替换架构。"]
        total = len(texts)
        done = failed = 0
        log_cb("【示例业务】批量文本摘要：语言=%s 风格=%s 最大字数=%s"
               % (inputs.get("lang"), inputs.get("style"), inputs.get("max_len")))
        for i, t in enumerate(texts, 1):
            if stop_event.is_set():
                log_cb("已结束任务，跳过剩余文本")
                break
            self.wait_pause(pause_event)
            time.sleep(0.7)
            phase = "summarize"
            progress_cb(done, total - done, t[:30], phase)
            log_cb("已摘要 第%d/%d 段 → %s…" % (i, total, t[:24]))
            done += 1
            progress_cb(done, total - done, t[:30], phase)
        log_cb("示例业务完成：处理 %d 段" % done)
        return {"total": total, "done": done, "failed": failed,
                "queue": total - done, "current": "", "phase": "完成"}


# ============================================================================
# 业务注册表
# ============================================================================
# dry_run 开关：False=真爬（调 main.run_langgraph_crawler 并落盘页面）。
# 可用环境变量 CRAWLER_DRY_RUN=true 临时切回演示模式（如现场演示不想真爬）。
import os as _os

DRY_RUN = _os.environ.get("CRAWLER_DRY_RUN", "false").lower() in ("1", "true", "yes")

RUNNERS = {
    CrawlerRunner.name: CrawlerRunner(dry_run=DRY_RUN),
    SampleRunner.name: SampleRunner(),
}


def get_runner(name: str) -> TaskRunner:
    return RUNNERS.get(name, CrawlerRunner())


def runner_names() -> List[str]:
    return list(RUNNERS.keys())
