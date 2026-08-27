"""
desktop_app.py — 任务工作台 · 校招版（pywebview/WebView2）

壳层（本文件 + webui/）只面向 runner_base.TaskRunner 接口，
业务可替换：切换顶部下拉即换业务，表单按 schema 自动重排。

运行：.venv_pwv\\Scripts\\python desktop_app.py
"""
import dataclasses
import json
import os
import threading

import webview

from runner_base import runner_names, get_runner, CrawlerRunner, PLATFORMS

CONFIG_FILE = "config.json"
WEBUI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui", "index.html")


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


class Api:
    """JS ↔ Python 桥。GUI 只认识 TaskRunner 接口，不感知具体业务。"""

    def __init__(self, window):
        self._window = window
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._cfg = _load_config()

    # ---- Python → JS ----
    def _js(self, code):
        try:
            self._window.evaluate_js(code)
        except Exception:
            pass

    def _log(self, msg, tag="info"):
        self._js("window.appendLog(%s, %s)" % (json.dumps(msg), json.dumps(tag)))

    def _summary(self, msg):
        self._js("window.appendSummary(%s)" % json.dumps(msg))

    # ---- 初始数据 / schema ----
    def get_initial(self):
        names = runner_names()
        first = names[0] if names else CrawlerRunner.name
        return {
            "runners": names,
            "schema": self.get_schema(first)["schema"],
            "saved_keys": self.list_saved_keys(),
            "last_inputs": self._cfg.get("last_inputs", {}),
        }

    def get_schema(self, name: str):
        runner = get_runner(name)
        fields = [dataclasses.asdict(f) for f in runner.fields]
        return {"name": runner.name, "schema": {"name": runner.name, "fields": fields}}

    def platform_fill(self, name: str):
        for p, base, model in PLATFORMS:
            if p == name:
                return {"name": p, "base_url": base, "model": model}
        return {"name": name, "base_url": "", "model": ""}

    # ---- 保存的 Key（全局能力，业务无关）----
    def list_saved_keys(self):
        keys = self._cfg.get("saved_keys", {}) or {}
        return [{"name": n, "key": k} for n, k in keys.items()]

    def save_key(self, name: str, key: str):
        keys = self._cfg.setdefault("saved_keys", {}) or {}
        keys[name.strip()] = key.strip()
        _save_config(self._cfg)
        return self.list_saved_keys()

    def delete_key(self, name: str):
        keys = self._cfg.get("saved_keys", {}) or {}
        keys.pop(name, None)
        _save_config(self._cfg)
        return self.list_saved_keys()

    # ---- 文件导入（真实系统对话框）----
    def select_file(self):
        try:
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("文本文件 (*.txt)", "所有文件 (*.*)"),
            )
            if result:
                return result[0]
        except Exception as e:
            self._log("打开文件对话框失败: %s" % e, "fail")
        return None

    # ---- 任务执行：只面向 TaskRunner ----
    def submit(self, payload: dict):
        runner_name = payload.pop("_runner", "")
        runner = get_runner(runner_name)
        inputs = {k: v for k, v in payload.items()}

        # 记住本次表单（下次启动自动回填）
        last = self._cfg.setdefault("last_inputs", {})
        last[runner.name] = inputs
        _save_config(self._cfg)

        self._log("业务：%s · 参数已接收" % runner.name, "info")
        self._stop.clear()
        self._pause.clear()

        def on_progress(done, queue, current, phase):
            total = done + queue
            percent = round(done * 100 / max(1, total))
            self._js(
                "window.setBar(%d); window.setBusy(false);"
                "window.setStats(%s);"
                "window.setStatus(%s)"
                % (percent, json.dumps({"total": total, "done": done, "queue": queue,
                                        "current": current[:44], "phase": phase}),
                   json.dumps("%s 正在处理 %s" % (phase, current[:40]))))

        def work():
            try:
                stats = runner.run(
                    inputs,
                    log_cb=self._log,
                    progress_cb=on_progress,
                    pause_event=self._pause,
                    stop_event=self._stop,
                )
                stats.setdefault("percent", 100)
                self._js("window.setStats(%s); window.setBar(100);"
                         "window.setBusy(false); window.setRunningUI(false);"
                         "window.setStatus('完成');"
                         % json.dumps(stats))
            except Exception as e:
                import traceback
                self._log("任务异常: %s\n%s" % (e, traceback.format_exc()[:400]), "fail")
                self._js("window.setBusy(false); window.setRunningUI(false);"
                         "window.setStatus('异常终止');")

        threading.Thread(target=work, daemon=True).start()
        return True

    # ---- 暂停 / 结束 ----
    def pause(self):
        self._pause.set()
        self._log("暂停信号已下发", "warn")
        return True

    def resume(self):
        self._pause.clear()
        self._log("已恢复", "pass")
        return True

    def stop(self):
        self._stop.set()
        self._log("结束信号已下发", "warn")
        return True

    def ping(self):
        return "pong"

    # ---- 结果可视化：页面列表 / HTML 预览 / 打包导出 ----
    # 输出目录结构: output/<netloc>/<栏目>/<标题>.html (+ crawl_results.csv)
    OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

    def _latest_result_dir(self):
        """output/ 下最新被写入的站点目录（含 html 文件），没有则返回 None。"""
        try:
            if not os.path.isdir(self.OUTPUT_ROOT):
                return None
            cands = []
            for name in os.listdir(self.OUTPUT_ROOT):
                p = os.path.join(self.OUTPUT_ROOT, name)
                if os.path.isdir(p):
                    html_cnt = sum(1 for _, _, fs in os.walk(p) for f in fs if f.lower().endswith(".html"))
                    if html_cnt:
                        cands.append((p, os.path.getmtime(p), html_cnt))
            if not cands:
                return None
            cands.sort(key=lambda t: t[1], reverse=True)
            return {"path": cands[0][0], "site": os.path.basename(cands[0][0]), "html_cnt": cands[0][2]}
        except Exception:
            return None

    def list_results(self):
        """扫描最新输出目录，返回页面列表。"""
        d = self._latest_result_dir()
        if not d:
            return {"site": "", "pages": []}
        pages = []
        for root, _, files in os.walk(d["path"]):
            for f in sorted(files):
                if not f.lower().endswith(".html"):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, d["path"]).replace("\\", "/")
                pages.append({
                    "rel": rel,
                    "title": os.path.splitext(f)[0],
                    "size": os.path.getsize(full),
                })
        return {"site": d["site"], "pages": pages}

    def read_page(self, rel: str):
        """读取某页 HTML 原文（供前端 iframe srcdoc 预览）。"""
        d = self._latest_result_dir()
        if not d:
            return ""
        full = os.path.join(d["path"], rel.replace("/", os.sep))
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except Exception:
            return ""

    def open_external(self, rel: str = ""):
        """用系统默认浏览器打开预览页对应的本地 HTML 文件（file://）。"""
        import webbrowser
        d = self._latest_result_dir()
        if not d or not rel:
            return False
        full = os.path.join(d["path"], rel.replace("/", os.sep))
        url = "file:///" + os.path.abspath(full).replace("\\", "/")
        webbrowser.open(url)
        return True

    def export_zip(self):
        """打包最新输出目录为 zip，返回 zip 绝对路径（前端 toast 提示并打开所在目录）。"""
        import zipfile
        d = self._latest_result_dir()
        if not d:
            return ""
        zip_path = d["path"] + "_result.zip"
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(d["path"]):
                    for f in files:
                        full = os.path.join(root, f)
                        arc = os.path.relpath(full, os.path.dirname(d["path"])).replace("\\", "/")
                        zf.write(full, arc)
            try:
                os.startfile(os.path.dirname(zip_path))  # 打开输出目录（含 zip）
            except Exception:
                pass
            return zip_path
        except Exception:
            return ""


def main():
    api = Api(None)
    window = webview.create_window(
        "任务工作台 · 校招版",
        url=WEBUI,
        js_api=api,
        width=880,
        height=860,
        background_color="#0a0a0b",
        text_select=False,
    )
    api._window = window
    webview.start(gui="edgechromium")


if __name__ == "__main__":
    main()
