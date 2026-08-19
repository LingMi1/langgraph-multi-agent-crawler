"""
网站爬取工具 v1.2 — GUI 入口（预检 + 多平台兼容）
通过 TXT 文件批量导入网址，逐站爬取并保存。
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import webbrowser
import sys
import os
import json
import time
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import site_crawler
import main as langgraph_main

CONFIG_FILE = "config.json"

PLATFORMS = [
    ("DeepSeek（深度求索）", "https://api.deepseek.com/v1", "deepseek-chat",
     "https://platform.deepseek.com/api_keys"),
    ("SiliconFlow（硅基流动）", "https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3",
     "https://cloud.siliconflow.cn/account/ak"),
    ("Moonshot（月之暗面）", "https://api.moonshot.cn/v1", "moonshot-v1-8k",
     "https://platform.moonshot.cn/console/api-keys"),
    ("智谱清言（GLM）", "https://open.bigmodel.cn/api/paas/v4", "glm-4",
     "https://open.bigmodel.cn/usercenter/apikeys"),
    ("通义千问（阿里云）", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo",
     "https://dashscope.console.aliyun.com/apiKey"),
    ("OpenAI 官方", "https://api.openai.com/v1", "gpt-4o-mini",
     "https://platform.openai.com/api-keys"),
    ("自定义", "", "", ""),
]
PLATFORM_NAMES = [p[0] for p in PLATFORMS]


class CrawlerGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("网站爬取工具 v1.2")
        self.root.geometry("680x660")
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.is_running = False
        self.should_stop = False
        self.stop_event = threading.Event()
        self.selected_file = tk.StringVar()
        self.api_key = tk.StringVar()
        self.base_url = tk.StringVar()
        self.model_name = tk.StringVar()
        self.platform_var = tk.StringVar()
        self.crawl_mode = tk.StringVar(value="traditional")

        # ★ 进度条页面级状态（LangGraph 模式使用）
        self._last_fetched = 0      # 当前站已处理页数（停止时保留显示用）
        self._site_index = 0        # 当前站点序号（1-based）
        self._site_total = 0        # 站点总数

        self._build_ui()
        self._auto_load_config()

    # ==================== UI ====================

    def _build_ui(self):
        # ── API 配置 ──
        api_frame = tk.LabelFrame(self.root, text="⚙️ API 配置（兼容 OpenAI 格式）", padx=8, pady=6)
        api_frame.pack(fill=tk.X, padx=10, pady=(10, 2))

        row0 = tk.Frame(api_frame); row0.pack(fill=tk.X, pady=1)
        tk.Label(row0, text="平台选择:", width=10, anchor="e").pack(side=tk.LEFT)
        self.platform_combo = ttk.Combobox(row0, textvariable=self.platform_var,
                                           values=PLATFORM_NAMES, state="readonly", width=22)
        self.platform_combo.pack(side=tk.LEFT, padx=5)
        self.platform_combo.bind("<<ComboboxSelected>>", self._on_platform_change)
        tk.Button(row0, text="一键填入", command=self._auto_fill, width=8).pack(side=tk.LEFT, padx=5)

        row1 = tk.Frame(api_frame); row1.pack(fill=tk.X, pady=1)
        tk.Label(row1, text="API 地址:", width=10, anchor="e").pack(side=tk.LEFT)
        self.base_url_entry = tk.Entry(row1, textvariable=self.base_url, width=52)
        self.base_url_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        row2 = tk.Frame(api_frame); row2.pack(fill=tk.X, pady=1)
        tk.Label(row2, text="OpenAI Key:", width=10, anchor="e").pack(side=tk.LEFT)
        self.key_entry = tk.Entry(row2, textvariable=self.api_key, width=36, show="*")
        self.key_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        self.btn_toggle = tk.Button(row2, text="👁", width=3, command=self._toggle_key_visibility)
        self.btn_toggle.pack(side=tk.LEFT, padx=2)
        tk.Button(row2, text="💾 保存配置", command=self._save_config, width=9).pack(side=tk.LEFT, padx=2)
        tk.Button(row2, text="🔗 获取Key", command=self._open_key_page, width=9).pack(side=tk.LEFT, padx=2)

        row3 = tk.Frame(api_frame); row3.pack(fill=tk.X, pady=1)
        tk.Label(row3, text="模型名称:", width=10, anchor="e").pack(side=tk.LEFT)
        self.model_entry = tk.Entry(row3, textvariable=self.model_name, width=52)
        self.model_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        # ── 文件选择 ──
        file_frame = tk.LabelFrame(self.root, text="📁 选择网址文件", padx=8, pady=6)
        file_frame.pack(fill=tk.X, padx=10, pady=(5, 2))
        tk.Label(file_frame, text="文件路径:", width=8, anchor="e").pack(side=tk.LEFT)
        self.file_entry = tk.Entry(file_frame, textvariable=self.selected_file, width=40)
        self.file_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        tk.Button(file_frame, text="浏览...", command=self._select_file, width=8).pack(side=tk.LEFT)
        tk.Label(self.root, text="  支持 .txt，每行一个网址，# 注释，最多 10 个", fg="gray", anchor="w").pack(fill=tk.X, padx=15)

        # ── 爬取模式选择 ──
        mode_frame = tk.LabelFrame(self.root, text="🔄 爬取模式", padx=8, pady=6)
        mode_frame.pack(fill=tk.X, padx=10, pady=(5, 2))
        mode_row = tk.Frame(mode_frame); mode_row.pack(fill=tk.X, pady=1)
        self.rb_traditional = tk.Radiobutton(mode_row, text="📋 传统模式（导航菜单分类目录）",
                                             variable=self.crawl_mode, value="traditional",
                                             command=self._on_mode_change)
        self.rb_traditional.pack(side=tk.LEFT, padx=5)
        self.rb_langgraph = tk.Radiobutton(mode_row, text="⚡ LangGraph 模式（面包屑多级目录）",
                                            variable=self.crawl_mode, value="langgraph",
                                            command=self._on_mode_change)
        self.rb_langgraph.pack(side=tk.LEFT, padx=5)
        self.rb_agent = tk.Radiobutton(mode_row, text="🤖 Agent 智能模式（Supervisor 多智能体，需要 API Key）",
                                        variable=self.crawl_mode, value="agent",
                                        command=self._on_mode_change)
        self.rb_agent.pack(side=tk.LEFT, padx=5)
        self.rb_multi = tk.Radiobutton(mode_row, text="🧠 Multi-Agent 模式（Scout→Nav→Fetch→Extract 流水线）",
                                        variable=self.crawl_mode, value="multi_agent",
                                        command=self._on_mode_change)
        self.rb_multi.pack(side=tk.LEFT, padx=5)
        self.rb_lg_ma = tk.Radiobutton(mode_row, text="⟐ LangGraph MA 模式（传统爬虫 + LLM 评估，StateGraph 架构）",
                                        variable=self.crawl_mode, value="langgraph_ma",
                                        command=self._on_mode_change)
        self.rb_lg_ma.pack(side=tk.LEFT, padx=5)
        self.mode_hint = tk.Label(mode_frame, text="📋 传统模式：导航菜单分类目录", fg="gray", anchor="w")
        self.mode_hint.pack(fill=tk.X, padx=5, pady=(2, 0))

        # ── 按钮 ──
        btn_frame = tk.Frame(self.root); btn_frame.pack(fill=tk.X, padx=10, pady=5)
        self.btn_start = tk.Button(btn_frame, text="🚀 开始爬取", command=self._start_crawl,
                                   width=14, bg="#4CAF50", fg="white")
        self.btn_start.pack(side=tk.LEFT, padx=5)
        self.btn_stop = tk.Button(btn_frame, text="⏹ 停止", command=self._stop_crawl,
                                  width=14, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=5)

        # ── 进度 + 日志 ──
        progress_frame = tk.LabelFrame(self.root, text="进度", padx=8, pady=6)
        progress_frame.pack(fill=tk.X, padx=10, pady=5)
        self.progress_bar = ttk.Progressbar(progress_frame, orient=tk.HORIZONTAL, mode='determinate')
        self.progress_bar.pack(fill=tk.X, pady=(0, 3))
        self.status_label = tk.Label(progress_frame, text="就绪", fg="gray", anchor="w")
        self.status_label.pack(fill=tk.X)

        # ── 主日志 ──
        log_frame = tk.LabelFrame(self.root, text="日志", padx=8, pady=6)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 2))
        tc = tk.Frame(log_frame); tc.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(tc, wrap=tk.WORD, state=tk.NORMAL, height=5)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = tk.Scrollbar(tc, command=self.log_text.yview); sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=sb.set)

        # ── ⛔ 不可爬取网址汇总 ──
        skip_frame = tk.LabelFrame(self.root, text="⛔ 不可爬取网址汇总", padx=8, pady=6)
        skip_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(2, 10))
        skip_tc = tk.Frame(skip_frame); skip_tc.pack(fill=tk.BOTH, expand=True)
        self.skip_text = tk.Text(skip_tc, wrap=tk.WORD, state=tk.NORMAL, height=4, fg="red")
        self.skip_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        skip_sb = tk.Scrollbar(skip_tc, command=self.skip_text.yview); skip_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.skip_text.config(yscrollcommand=skip_sb.set)

        # 颜色标签
        self.log_text.tag_configure("pass", foreground="green")
        self.log_text.tag_configure("fail", foreground="red")
        self.log_text.tag_configure("info", foreground="blue")
        self.log_text.tag_configure("warn", foreground="#E67E22")  # 橙色反爬警告

    # ==================== 平台联动 ====================

    def _on_platform_change(self, event=None):
        name = self.platform_var.get()
        for pname, burl, model, _ in PLATFORMS:
            if pname == name:
                self.base_url.set(burl); self.model_name.set(model); return

    def _auto_fill(self):
        name = self.platform_var.get()
        for pname, burl, model, _ in PLATFORMS:
            if pname == name:
                self.base_url.set(burl); self.model_name.set(model)
                self.log(f"已填入 {pname} 配置"); return

    def _open_key_page(self):
        name = self.platform_var.get()
        url = ""
        for pname, _, _, key_url in PLATFORMS:
            if pname == name: url = key_url; break
        webbrowser.open(url or "https://platform.deepseek.com/api_keys")

    def _toggle_key_visibility(self):
        c = self.key_entry.cget("show")
        self.key_entry.config(show="" if c == "*" else "*")
        self.btn_toggle.config(text="🔒" if c == "*" else "👁")

    def _save_config(self):
        key = self.api_key.get().strip()
        if not key: messagebox.showwarning("警告", "请先输入 OpenAI Key"); return
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"platform": self.platform_var.get(), "base_url": self.base_url.get().strip(),
                           "api_key": key, "model_name": self.model_name.get().strip()},
                          f, ensure_ascii=False, indent=2)
            messagebox.showinfo("成功", "✅ 配置已保存"); self.log("配置已保存")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败: {e}")

    def _auto_load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f: cfg = json.load(f)
                api_key_v = cfg.get("api_key", "")
                base_url_v = cfg.get("base_url", "")
                model_v = cfg.get("model_name", "")
                self.api_key.set(api_key_v); self.base_url.set(base_url_v)
                self.model_name.set(model_v)
                plat = cfg.get("platform", "")
                if plat in PLATFORM_NAMES: self.platform_var.set(plat)
                # ★ 同步注入 config 模块，确保启动时 LLM 配置就绪
                import config as _cfg
                _cfg.DEEPSEEK_API_KEY = api_key_v
                _cfg.DEEPSEEK_BASE_URL = base_url_v
                _cfg.DEEPSEEK_MODEL = model_v
                self.log("已加载 config.json 配置")
            except Exception: pass

    # ==================== 文件选择 ====================

    def _select_file(self):
        fp = filedialog.askopenfilename(title="选择网址文件", filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if fp: self.selected_file.set(fp); self.log(f"已选择: {os.path.basename(fp)}")

    def _parse_urls(self, filepath):
        urls = []
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): continue
                if not line.startswith(('http://', 'https://')):
                    self.log(f"⚠️ 跳过无效网址: {line}"); continue
                urls.append(line)
        seen = set(); unique = []
        for u in urls:
            n = u.rstrip("/")
            if n not in seen: seen.add(n); unique.append(u)
        if len(unique) > 10:
            messagebox.showwarning("网址过多", f"{len(unique)} 个网址，最多10个，只取前10个")
            unique = unique[:10]
        return unique

    # ==================== 模式切换 ====================

    def _on_mode_change(self):
        mode = self.crawl_mode.get()
        if mode == "langgraph_ma":
            self.mode_hint.config(text="⟐ LangGraph MA 模式：传统爬虫为主 + LLM 评估为辅（StateGraph 条件路由，最多3轮自动调整）")
        elif mode == "multi_agent":
            self.mode_hint.config(text="🧠 Multi-Agent 模式：Scout→Nav→Fetch→Extract 异步流水线（不依赖 LLM，基于规则 + trafilatura）")
        elif mode == "agent":
            self.mode_hint.config(text="🤖 Agent 智能模式：Supervisor 多智能体自动决策（需要配置 API Key）")
        elif mode == "langgraph":
            self.mode_hint.config(text="⚡ LangGraph 模式：支持面包屑多级目录解析")
        else:
            self.mode_hint.config(text="📋 传统模式：导航菜单分类目录")

    # ==================== 爬取控制 ====================

    def _start_crawl(self):
        if self.is_running: return

        filepath = self.selected_file.get().strip()
        if not filepath: messagebox.showerror("错误", "请先选择 .txt 文件"); return
        if not os.path.exists(filepath): messagebox.showerror("错误", "文件不存在"); return

        urls = self._parse_urls(filepath)
        if not urls: messagebox.showerror("错误", "没有有效网址"); return

        api_key = self.api_key.get().strip()
        base_url = self.base_url.get().strip()
        model_name = self.model_name.get().strip()

        # API Key 可选化：Multi-Agent / 传统模式不需要 LLM
        has_llm = bool(api_key and base_url and model_name)
        mode = self.crawl_mode.get()
        if mode in ("multi_agent", "langgraph_ma"):
            if has_llm:
                self.log("ℹ️ 已配置 API Key，LLM 评估/深降级将使用")
            else:
                self.log("ℹ️ 未配置 API Key，将使用启发式评估（无需 LLM）")
        elif not has_llm:
            self.log("ℹ️ 未配置 API Key，将使用默认爬取模式（不调用大模型清洗）")

        self.is_running = True; self.should_stop = False
        self.stop_event.clear()
        self.btn_start.config(state=tk.DISABLED); self.btn_stop.config(state=tk.NORMAL)
        self.log_text.delete('1.0', tk.END); self.skip_text.delete('1.0', tk.END)

        # 打印全局输出根目录
        output_root = os.path.abspath("output")
        self.log(f"📂 [系统提示] 本次爬取的数据将保存至: {output_root}")

        mode = self.crawl_mode.get()
        mode_label = {"agent": "🤖 Agent 智能", "langgraph": "⚡ LangGraph",
                      "traditional": "📋 传统", "multi_agent": "🧠 Multi-Agent",
                      "langgraph_ma": "⟐ LangGraph MA"}.get(mode, mode)
        self.log(f"{mode_label}模式 — 共加载 {len(urls)} 个网址，开始预检...")

        threading.Thread(target=self._precheck_worker, args=(urls, api_key, base_url, model_name), daemon=True).start()

    def _stop_crawl(self):
        self.should_stop = True
        self.stop_event.set()
        self.log("⏹ 用户请求停止...")
        self.btn_stop.config(state=tk.DISABLED)

    # ==================== 预检阶段 ====================

    def _precheck_worker(self, urls, api_key, base_url, model_name):
        valid_urls = []
        skipped = []

        for i, url in enumerate(urls):
            if self.should_stop:
                self.log("⏹ 预检已停止"); break

            self._update_progress(i, len(urls), f"深度预检中: {url[:60]}")
            self.log(f"🔍 深度预检 ({i+1}/{len(urls)}): {url[:70]}", "info")

            result = site_crawler.deep_pre_check(url)
            if result["pass"]:
                valid_urls.append(url)
                self.log(f"✅ {url[:70]} - 预检通过", "pass")
            else:
                skipped.append(result)
                self.log(f"⛔ {url[:70]} - {result['reason']}", "fail")

        # 更新跳过汇总面板
        if skipped:
            lines = []
            for s in skipped:
                lines.append(f"⛔ {s['url']}\n   原因：{s['reason']}\n")
            self.root.after(0, lambda: self.skip_text.insert(tk.END, "\n".join(lines)))
        else:
            self.root.after(0, lambda: self.skip_text.insert(tk.END, "✅ 所有网址预检通过，均可正常爬取"))

        self.log(f"预检完成：通过 {len(valid_urls)} 个，跳过 {len(skipped)} 个")

        if not valid_urls:
            self.log("❌ 所有网址均未通过预检，无任务可执行")
            self.root.after(0, lambda: messagebox.showinfo("预检结果", "所有网址均未通过预检，无任务可执行"))
            self.root.after(0, lambda: self._reset_ui(
                complete=False,
                label="无任务" if not self.should_stop else "已停止"))
            return

        # 开始实际爬取
        self._crawl_worker(valid_urls, api_key, base_url, model_name)

    # ==================== 爬取阶段 ====================

    def _crawl_worker(self, urls, api_key, base_url, model_name):
        total_targets = len(urls)
        start_time = time.time()
        success = 0; failed = 0
        summary = []
        mode = self.crawl_mode.get()
        use_langgraph = (mode == "langgraph")
        use_agent = (mode == "agent")
        use_multi = (mode == "multi_agent")
        use_lg_ma = (mode == "langgraph_ma")

        for i, url in enumerate(urls):
            if self.should_stop:
                self.log(f"⏹ 已停止，跳过剩余 {len(urls) - i} 个"); break

            # 目标切换边界
            self.log(f"\n{'='*20} 开始处理第 {i+1}/{total_targets} 个目标 {'='*20}")
            self.log(f"▶ 目标网址: {url}")
            # ★ 进度条按「当前站内页面数」推进：进入新站时重置为 0，
            #   由 LangGraph 进度回调（_lg_progress）逐页上报；跨站靠文案序号区分。
            self._site_index = i + 1
            self._site_total = total_targets
            self._last_fetched = 0
            self._update_progress(0, 1, f"爬取中 第{i+1}/{total_targets}个站: {url[:50]}")

            site_dir = None  # 记录当前网站的保存路径
            pages = 0
            company_name = ""
            try:
                if use_lg_ma:
                    # ⟐ LangGraph MA 模式：传统爬虫为主 + LLM 评估（StateGraph 条件路由）
                    self.log(f"⟐ [LangGraph MA 模式] 启动 StateGraph 工作流...", "info")

                    if api_key:
                        os.environ["DEEPSEEK_API_KEY"] = api_key
                        os.environ["DEEPSEEK_BASE_URL"] = base_url
                        os.environ["DEEPSEEK_MODEL"] = model_name
                        import config as _cfg
                        _cfg.DEEPSEEK_API_KEY = api_key
                        _cfg.DEEPSEEK_BASE_URL = base_url
                        _cfg.DEEPSEEK_MODEL = model_name

                    # ★ 强制重置 LangGraph MA 的 LLM 单例，确保改 key/base_url 后不重启也能生效
                    from graph import nodes as _gn
                    _gn.reset_llm()
                    from agents import extractor as _ext
                    _ext.reset_llm_client()

                    pages = langgraph_main.run_langgraph_crawler(
                        url,
                        concurrency=5,
                        log_callback=self._langgraph_log,
                        reset_memory=False,
                        progress_callback=self._lg_progress,
                    ) or 0

                    # 推算输出目录（scout_node 创建的是 output/<netloc>，保留 www）
                    parsed_base = urlparse(url)
                    domain = parsed_base.netloc.replace(":", "_")
                    site_dir = os.path.join("output", domain)
                    company_name = domain

                elif use_multi:
                    # 🧠 Multi-Agent 模式：Scout→Nav→Fetcher→Extractor→Storage 异步流水线
                    self.log(f"🧠 [Multi-Agent 模式] 启动异步流水线 (并发={5})...", "info")

                    # 注入 API 配置（ExtractorAgent LLM 深降级时使用）
                    if api_key:
                        import config as _cfg
                        _cfg.DEEPSEEK_API_KEY = api_key
                        _cfg.DEEPSEEK_BASE_URL = base_url
                        _cfg.DEEPSEEK_MODEL = model_name
                        os.environ["DEEPSEEK_API_KEY"] = api_key
                        os.environ["DEEPSEEK_BASE_URL"] = base_url
                        os.environ["DEEPSEEK_MODEL"] = model_name

                    # ★ 强制重置 ExtractorAgent 的 LLM 单例，确保改 key/base_url 后不重启也能生效
                    from agents import extractor as _ext
                    _ext.reset_llm_client()

                    pages = langgraph_main.run_multi_agent(
                        url,
                        concurrency=5,
                        log_callback=self._langgraph_log,
                        reset_memory=False,
                    ) or 0

                    # 推算输出目录（scout_node 创建的是 output/<netloc>，保留 www）
                    parsed_base = urlparse(url)
                    domain = parsed_base.netloc.replace(":", "_")
                    site_dir = os.path.join("output", domain)
                    company_name = domain

                elif use_agent:
                    # 🤖 Agent 模式：通过环境变量注入 API 配置，调用 Supervisor Agent
                    self.log(f"🤖 [Agent模式] 启动 Supervisor 多智能体...", "info")
                    if api_key:
                        os.environ["DEEPSEEK_API_KEY"] = api_key
                    if base_url:
                        os.environ["DEEPSEEK_BASE_URL"] = base_url
                    if model_name:
                        os.environ["DEEPSEEK_MODEL"] = model_name
                    
                    # ★ 直接修改 config 模块变量，避免模块级变量已在导入时求值为空
                    import config as _cfg
                    _cfg.DEEPSEEK_API_KEY = api_key
                    _cfg.DEEPSEEK_BASE_URL = base_url
                    _cfg.DEEPSEEK_MODEL = model_name
                    # ★ 强制重置 LLM 单例，确保使用新配置
                    import supervisor as _sv
                    _sv.reset_supervisor_llm()
                    import workers as _wk
                    _wk.reset_worker_llm()

                    # 预检获取公司名创建输出目录
                    check_result = site_crawler.deep_pre_check(url)
                    if check_result.get("pass") and check_result.get("html"):
                        try:
                            parsed_base = urlparse(url)
                            domain = parsed_base.netloc.replace("www.", "").replace(":", "_")
                            soup = BeautifulSoup(check_result["html"], "html.parser")
                            company_name = site_crawler._safe_filename(
                                site_crawler.extract_company_name(soup, domain))
                        except Exception:
                            company_name = site_crawler._safe_filename(domain)
                    else:
                        parsed_base = urlparse(url)
                        company_name = site_crawler._safe_filename(
                            parsed_base.netloc.replace("www.", "").replace(":", "_"))

                    site_dir = os.path.join("output", company_name)
                    os.makedirs(site_dir, exist_ok=True)
                    self.log(f"📁 Agent 输出目录: {site_dir}", "info")

                    pages = langgraph_main.run_agent(
                        url,
                        log_callback=self._langgraph_log,
                        stop_event=self.stop_event,
                        site_dir=site_dir
                    ) or 0
                elif use_langgraph:
                    # ⚡ LangGraph 模式：深度反爬预检 + 熔断跳过机制

                    # ---- 第1步：深度预检（在建目录之前，防反爬假200 OK） ----
                    check_result = site_crawler.deep_pre_check(url)
                    if not check_result["pass"]:
                        self.log(
                            f"⛔ [LangGraph模式] 预检失败，跳过该网站 [{url}]\n"
                            f"   原因: {check_result['reason']}",
                            "fail"
                        )
                        summary.append(f"⛔ {url[:50]} - 预检失败: {check_result['reason'][:30]}")
                        failed += 1
                        # 记录到跳过汇总
                        self.root.after(0, lambda r=check_result: self.skip_text.insert(
                            tk.END, f"⛔ {r['url']}\n   原因：{r['reason']}\n"))
                        continue  # 跳过！不建目录、不调 LangGraph

                    # ---- 第2步：预检通过，从 HTML 提取公司名并创建输出目录 ----
                    pre_html = check_result.get("html", "")
                    parsed_base = urlparse(url)
                    domain = parsed_base.netloc.replace("www.", "").replace(":", "_")
                    try:
                        soup = BeautifulSoup(pre_html, "html.parser")
                        company_name = site_crawler._safe_filename(
                            site_crawler.extract_company_name(soup, domain)
                        )
                    except Exception:
                        company_name = site_crawler._safe_filename(domain)

                    site_dir = os.path.join("output", company_name)
                    os.makedirs(site_dir, exist_ok=True)
                    self.log(f"📁 LangGraph 输出目录: {site_dir}", "info")

                    # ---- 第3步：调用 LangGraph 入口函数 ----
                    pages = langgraph_main.run_langgraph(
                        url,
                        log_callback=self._langgraph_log,
                        stop_event=self.stop_event,
                        site_dir=site_dir
                    ) or 0
                else:
                    # 📋 传统模式：site_crawler.main 内部是 try-except 保护的 crawl()
                    pages = site_crawler.main(url, api_key, base_url, model_name) or 0
                    # 传统模式：根据 URL 推算出保存路径（与 site_crawler.crawl 中的逻辑一致）
                    parsed_base = urlparse(url)
                    domain = parsed_base.netloc.replace("www.", "").replace(":", "_")
                    # 尝试读取首页 HTML 提取公司名（与 crawl 内逻辑一致）
                    try:
                        from bs4 import BeautifulSoup
                        import site_crawler as sc
                        raw_html = sc.fetch(url)
                        if raw_html:
                            soup = BeautifulSoup(raw_html, "html.parser")
                            company_name = sc._safe_filename(sc.extract_company_name(soup, domain))
                        else:
                            company_name = sc._safe_filename(domain)
                    except Exception:
                        company_name = site_crawler._safe_filename(domain)
                    site_dir = os.path.join("output", company_name)

                # 打印完成日志，含具体保存路径
                if site_dir:
                    abs_site_dir = os.path.abspath(site_dir)
                    self.log(f"✅ [完成] {company_name} 爬取结束，文件已保存至: {abs_site_dir}", "pass")
                self.log(f"✅ [{i+1}/{total_targets}] {url[:70]} 完成 (爬取 {pages} 页)", "pass")
                summary.append(f"✅ {url[:50]} - {pages} 页")
                success += 1
            except Exception as e:
                import traceback
                self.log(f"❌ 致命错误: {url[:70]} 异常退出: {e}", "fail")
                self.log(traceback.format_exc()[:500], "fail")
                self.log(f"⚠️ 已跳过 {url[:50]}，继续处理下一个目标...")
                summary.append(f"❌ {url[:50]} - 异常: {str(e)[:40]}")
                failed += 1

            self.log(f"{'='*50}")
            time.sleep(1)

        elapsed = time.time() - start_time
        mins, secs = divmod(int(elapsed), 60)

        # 输出汇总
        self.log(f"\n{'='*20} 📊 任务最终汇总 {'='*20}")
        self.log(f"总计输入目标: {total_targets} 个")
        self.log(f"成功: {success} 个, 失败: {failed} 个")
        for s in summary:
            self.log(f"  {s}")
        self.log(f"总耗时: {mins}分{secs}秒")
        self.log(f"{'='*50}")
        if self.should_stop:
            self.root.after(0, lambda: self._reset_ui(complete=False))
        else:
            self.root.after(0, self._reset_ui)

    def _lg_progress(self, fetched, queue_len, url, phase="fetch"):
        """LangGraph 进度回调：fetched=已处理页数, queue_len=剩余/总数页数"""
        self._last_fetched = fetched
        total = fetched + queue_len
        if phase == "media":
            label = f"内嵌图片 {fetched}/{total} 页"
        elif phase == "storage":
            label = "写入文件..."
        else:
            label = f"爬取中 第{self._site_index}/{self._site_total}个站 · 已处理{fetched}页 剩{queue_len}页"
        self._update_progress(fetched, total, label)

    def _langgraph_log(self, msg: str):
        """LangGraph 工作流的日志回调，转发到 GUI 日志窗口。自动识别反爬拦截日志"""
        tag = "info"
        if "❌" in msg or "失败" in msg or "fail" in msg.lower():
            tag = "fail"
        elif "✅" in msg or "通过" in msg or "pass" in msg.lower():
            tag = "pass"
        elif "🛡️" in msg or "⛔" in msg or "反爬" in msg:
            tag = "warn"
        self.log(msg, tag)

    def _reset_ui(self, complete: bool = True, label: str = ""):
        self.is_running = False
        self.btn_start.config(state=tk.NORMAL); self.btn_stop.config(state=tk.DISABLED)
        if complete:
            self.progress_bar['value'] = 100
            self.status_label.config(text="完成")
        else:
            # ★ 停止/无任务：保留实际进度，不假报 100%
            self.status_label.config(text=label or f"已停止（处理到第 {self._last_fetched} 页）")

    # ==================== 日志与进度 ====================

    def log(self, msg, tag=None):
        t = datetime.now().strftime("%H:%M:%S")
        self.root.after(0, lambda: self._append_log(f"[{t}] {msg}\n", tag))

    def _append_log(self, msg, tag):
        if tag:
            self.log_text.insert(tk.END, msg, tag)
        else:
            self.log_text.insert(tk.END, msg)
        self.log_text.see(tk.END)

    def _update_progress(self, current, total, label):
        self.root.after(0, lambda: self._set_progress(current, total, label))

    def _set_progress(self, c, t, lb):
        self.progress_bar['value'] = (c / t) * 100 if t > 0 else 0
        self.status_label.config(text=lb)

    def _on_close(self):
        if self.is_running:
            if not messagebox.askyesno("确认退出", "爬取进行中，确定退出？"): return
            self.should_stop = True
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    CrawlerGUI().run()