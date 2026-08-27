"""
风格A·Linear 精密工具风 试点壳 v1.5 — pywebview(WebView2) 演示
布局：双栏工作台（解决"功能多要下滑"）
  顶部固定：标题 + 控制按钮 + 进度条 + 状态
  左栏：API 配置 + 网址输入（窄栏，静态）
  右栏：日志 / 不可爬取汇总（各自内部滚动，页面不滚）
设计：Linear（近黑画布 + 发丝边框 + 单一靛蓝 + 克制动效）
字体：Space Grotesk / Noto Sans SC / JetBrains Mono
功能：文件导入 / 保存的 Key 列举 / 暂停·恢复·结束任务 / JS↔Python 桥接

运行：.venv_pwv\\Scripts\\python style_a_demo.py
"""
import json
import threading
import time

import webview

CONFIG_FILE = "config.json"


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&family=Noto+Sans+SC:wght@400;500&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  :root {
    --canvas: #0a0a0b;
    --surface-1: #0f1011;
    --surface-2: #141516;
    --surface-3: #191a1b;
    --hairline: rgba(255,255,255,.05);
    --hairline-strong: rgba(255,255,255,.12);
    --text-1: #f7f8f8;
    --text-2: #d0d6e0;
    --text-3: #8a8f98;
    --text-4: #62666d;
    --accent: #5e6ad2;
    --accent-hover: #828fff;
    --accent-ring: rgba(94,106,210,.35);
    --success: #27a644;
    --error: #eb5757;
    --warn: #f2c94c;
    --radius-sm: 6px;
    --radius-md: 8px;
    --radius-lg: 12px;
    --ease: cubic-bezier(.2, 0, 0, 1);
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; overflow: hidden; }
  body {
    font-family: "Space Grotesk", "Noto Sans SC", "Microsoft YaHei", sans-serif;
    background: var(--canvas);
    color: var(--text-2);
    user-select: none;
  }
  .app { display: flex; flex-direction: column; height: 100vh; padding: 18px 20px 20px; }

  /* ---- 顶栏：固定 ---- */
  header { display: flex; align-items: center; gap: 16px; flex-shrink: 0; }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand h1 { font-size: 16px; font-weight: 500; letter-spacing: -0.02em; color: var(--text-1); }
  .ver {
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 10.5px; letter-spacing: .06em; text-transform: uppercase;
    color: var(--text-3);
    background: var(--surface-2);
    border: 1px solid var(--hairline);
    border-radius: var(--radius-sm);
    padding: 2px 8px;
  }
  .controls { margin-left: auto; display: flex; align-items: center; gap: 8px; }
  .btn {
    border-radius: var(--radius-md);
    padding: 8px 15px;
    font-size: 13px;
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
    border: 1px solid transparent;
    transition: background-color .15s var(--ease), border-color .15s var(--ease),
                color .15s var(--ease), transform .1s var(--ease);
  }
  .btn:active { transform: translateY(1px); }
  .btn-start { background: var(--accent); color: #fff; }
  .btn-start:hover { background: var(--accent-hover); }
  .btn-start:disabled { background: rgba(255,255,255,.06); color: var(--text-4); cursor: not-allowed; }
  .btn-pause { background: transparent; border-color: var(--hairline-strong); color: var(--text-2); }
  .btn-pause:hover { background: var(--surface-2); border-color: rgba(255,255,255,.22); }
  .btn-pause.resume { border-color: rgba(242,201,76,.35); color: var(--warn); }
  .btn-stop { background: transparent; border-color: rgba(235,87,87,.35); color: var(--error); }
  .btn-stop:hover { background: rgba(235,87,87,.08); border-color: rgba(235,87,87,.5); }
  .btn:disabled { color: var(--text-4); border-color: var(--hairline-strong); background: transparent; cursor: not-allowed; }
  .kbd-hints { display: flex; gap: 12px; font-size: 11.5px; color: var(--text-4); margin-left: 8px; }
  kbd {
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 10.5px;
    color: var(--text-3);
    background: var(--surface-2);
    border: 1px solid var(--hairline-strong);
    border-radius: 4px;
    padding: 2px 5px;
  }

  /* ---- 状态条：固定 ---- */
  .statusbar { margin-top: 14px; flex-shrink: 0; }
  .bar { height: 4px; background: var(--surface-3); border-radius: 2px; overflow: hidden; }
  .bar-fill { height: 100%; width: 0%; background: var(--accent); border-radius: 2px; transition: width .3s var(--ease); }
  .status { margin-top: 6px; font-size: 12px; color: var(--text-3); }
  .status .mono { font-family: "JetBrains Mono", Consolas, monospace; font-size: 11px; color: var(--text-4); }

  /* ---- 主体：双栏 ---- */
  main { flex: 1; display: flex; gap: 16px; margin-top: 16px; min-height: 0; }

  /* 左栏：配置（窄，静态） */
  .col-config { width: 316px; flex-shrink: 0; overflow-y: auto; }
  .col-config::-webkit-scrollbar { width: 6px; }
  .col-config::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 3px; }

  /* 右栏：运行（宽，日志区滚动） */
  .col-run { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; gap: 12px; }

  .panel {
    background: var(--surface-1);
    border: 1px solid var(--hairline);
    border-radius: var(--radius-lg);
    padding: 14px 15px;
    box-shadow: inset 0 1px 0 rgba(255,255,255,.03);
  }
  .eyebrow {
    display: block;
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 10.5px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase;
    color: var(--text-4);
    margin-bottom: 12px;
  }
  .row { margin-bottom: 10px; }
  .row:last-child { margin-bottom: 0; }
  .row label { display: block; font-size: 11.5px; color: var(--text-3); margin-bottom: 4px; }

  /* ---- 输入：Linear text-input + 风格A 收敛发光 ---- */
  .field {
    background: var(--surface-1);
    border: 1px solid var(--hairline-strong);
    padding: 7px 10px;
    font-size: 12.5px;
    font-family: inherit;
    width: 100%;
    border-radius: var(--radius-md);
    color: var(--text-1);
    transition: border-color .15s var(--ease), box-shadow .15s var(--ease),
                background-color .15s var(--ease);
  }
  .field::placeholder { color: var(--text-4); }
  .field:hover { border-color: rgba(255,255,255,.22); background: var(--surface-2); }
  .field:focus {
    outline: none;
    border-color: var(--accent);
    background: var(--surface-2);
    box-shadow: 0 0 0 2px var(--accent-ring), 0 0 0 1px rgba(94,106,210,.55), 0 0 16px rgba(94,106,210,.16);
  }
  textarea.field { height: 56px; resize: none; line-height: 1.55; }

  select.field {
    appearance: none; -webkit-appearance: none;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><path d='M0 0l5 6 5-6z' fill='%2362666d'/></svg>");
    background-repeat: no-repeat;
    background-position: right 11px center;
    padding-right: 28px;
    cursor: pointer;
  }
  select.field option { background: var(--surface-1); color: var(--text-1); }

  .inline { display: flex; gap: 7px; }
  .inline .grow { flex: 1; }
  .btn-ghost {
    background: transparent;
    border: 1px solid var(--hairline-strong);
    border-radius: var(--radius-md);
    color: var(--text-3);
    font-size: 12px;
    font-family: inherit;
    padding: 0 10px;
    cursor: pointer;
    white-space: nowrap;
    transition: color .15s var(--ease), background-color .15s var(--ease), border-color .15s var(--ease);
  }
  .btn-ghost:hover { color: var(--text-1); background: var(--surface-2); border-color: rgba(255,255,255,.22); }
  .btn-ghost.danger:hover { color: var(--error); border-color: rgba(235,87,87,.4); }

  /* ---- 运行区面板 ---- */
  .log-panel, .skip-panel { display: flex; flex-direction: column; min-height: 0; }
  .log-panel { flex: 1; }
  .skip-panel { flex-shrink: 0; height: 148px; }
  .log-box {
    flex: 1;
    min-height: 0;
    background: #0d0e0f;
    border: 1px solid var(--hairline);
    border-radius: var(--radius-md);
    padding: 10px 12px;
    overflow-y: auto;
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 11.5px;
    line-height: 1.8;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .skip-box {
    flex: 1;
    min-height: 0;
    background: #0d0e0f;
    border: 1px solid rgba(235,87,87,.15);
    border-radius: var(--radius-md);
    padding: 9px 12px;
    overflow-y: auto;
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 11px;
    line-height: 1.75;
    color: var(--error);
  }
  .log-box .info { color: var(--text-3); }
  .log-box .pass { color: var(--success); }
  .log-box .fail { color: var(--error); }
  .log-box .warn { color: var(--warn); }
  .log-box .ts { color: var(--text-4); }

  ::-webkit-scrollbar { width: 8px; }
  ::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 4px; }
  ::-webkit-scrollbar-thumb:hover { background: #24262a; }
</style>
</head>
<body>
  <div class="app">

    <header>
      <div class="brand">
        <h1>网站爬取工具</h1>
        <span class="ver">v1.5 · 双栏</span>
      </div>
      <div class="controls">
        <button class="btn btn-start" id="btn_start" disabled>开始爬取</button>
        <button class="btn btn-pause" id="btn_pause" disabled>暂停</button>
        <button class="btn btn-stop" id="btn_stop" disabled>结束任务</button>
        <span class="kbd-hints"><span><kbd>Enter</kbd>开始</span><span><kbd>Esc</kbd>结束</span></span>
      </div>
    </header>

    <div class="statusbar">
      <div class="bar"><div class="bar-fill" id="bar_fill"></div></div>
      <div class="status" id="status">就绪 · 试点壳演示（不真正爬取）</div>
    </div>

    <main>
      <!-- 左栏：配置 -->
      <aside class="col-config">
        <section class="panel">
          <span class="eyebrow">API Configuration</span>

          <div class="row"><label>平台选择</label>
            <div class="inline">
              <select class="field grow" id="platform">
                <option>DeepSeek（深度求索）</option>
                <option>SiliconFlow（硅基流动）</option>
                <option>Moonshot（月之暗面）</option>
                <option>智谱清言（GLM）</option>
                <option>通义千问（阿里云）</option>
                <option>OpenAI 官方</option>
                <option>自定义</option>
              </select>
              <button class="btn-ghost" id="btn_fill">填入</button>
            </div>
          </div>

          <div class="row"><label>API 地址</label>
            <input class="field" id="base_url" type="text" placeholder="https://api.deepseek.com/v1">
          </div>

          <div class="row"><label>OpenAI Key</label>
            <div class="inline">
              <input class="field grow" id="api_key" type="password" placeholder="sk-...">
              <button class="btn-ghost" id="btn_eye" title="显示/隐藏">👁</button>
            </div>
          </div>

          <div class="row"><label>保存的 Key</label>
            <div class="inline">
              <select class="field grow" id="saved_keys">
                <option value="">— 选择已保存 —</option>
              </select>
              <button class="btn-ghost danger" id="btn_del_key">删</button>
            </div>
          </div>

          <div class="row"><label>保存当前 Key 为</label>
            <div class="inline">
              <input class="field grow" id="key_name" type="text" placeholder="名称，如 deepseek-生产">
              <button class="btn-ghost" id="btn_save_key">存</button>
            </div>
          </div>

          <div class="row"><label>模型名称</label>
            <input class="field" id="model_name" type="text" placeholder="deepseek-chat">
          </div>
        </section>

        <section class="panel" style="margin-top:12px">
          <span class="eyebrow">Input URLs</span>

          <div class="row"><label>导入网址文件（.txt，每行一个）</label>
            <div class="inline">
              <input class="field grow" id="file_path" type="text" placeholder="未选择文件" readonly>
              <button class="btn-ghost" id="btn_browse">浏览…</button>
            </div>
          </div>

          <div class="row"><label>或直接输入网址（空格/逗号/换行分隔，优先于文件）</label>
            <textarea class="field" id="urls" placeholder="https://example.com&#10;https://example2.com"></textarea>
          </div>
        </section>
      </aside>

      <!-- 右栏：运行 -->
      <section class="col-run">
        <div class="panel log-panel">
          <span class="eyebrow">Run Log</span>
          <div class="log-box" id="log"></div>
        </div>
        <div class="panel skip-panel">
          <span class="eyebrow">Skipped URLs</span>
          <div class="skip-box" id="skip_box">⛔ 不可爬取网址汇总（运行后显示）</div>
        </div>
      </section>
    </main>

  </div>

<script>
  var running = false;
  var paused = false;
  var bridgeReady = false;
  var savedKeys = [];

  function $(id) { return document.getElementById(id); }

  function appendLog(msg, tag) {
    var t = new Date().toTimeString().slice(0, 8);
    var div = document.createElement('div');
    div.className = tag || 'info';
    div.innerHTML = '<span class="ts">[' + t + ']</span> ' + msg;
    $('log').appendChild(div);
    $('log').scrollTop = $('log').scrollHeight;
  }
  function setStatus(s) { $('status').textContent = s; }
  function setBar(pct) { $('bar_fill').style.width = pct + '%'; }
  function renderSavedKeys() {
    var sel = $('saved_keys');
    sel.innerHTML = '<option value="">— 选择已保存 —</option>';
    savedKeys.forEach(function (k) {
      var o = document.createElement('option');
      o.value = k.key;
      o.textContent = k.name + '  ·  ' + k.key.slice(0, 10) + '…';
      sel.appendChild(o);
    });
  }
  function setRunningUI(on) {
    $('btn_start').disabled = on;
    $('btn_pause').disabled = !on;
    $('btn_stop').disabled = !on;
    if (!on) { paused = false; $('btn_pause').textContent = '暂停'; $('btn_pause').classList.remove('resume'); }
  }

  function toggleReady() {
    if (window.pywebview && window.pywebview.api) {
      bridgeReady = true;
      $('btn_start').disabled = false;
      appendLog('pywebview 桥接就绪，可以开始（试点壳仅演示数据通道，不真正爬取）', 'pass');
    }
  }
  window.addEventListener('pywebviewready', toggleReady);
  setTimeout(toggleReady, 800);

  async function loadInitial() {
    if (!bridgeReady) return;
    var d = await window.pywebview.api.get_initial();
    if (d.platform) $('platform').value = d.platform;
    if (d.base_url) $('base_url').value = d.base_url;
    if (d.model) $('model_name').value = d.model;
    savedKeys = d.saved_keys || [];
    renderSavedKeys();
    appendLog('已加载 config.json 配置，保存的 Key ' + savedKeys.length + ' 个', 'info');
  }

  $('btn_fill').onclick = async function () {
    if (!bridgeReady) return;
    var d = await window.pywebview.api.platform_fill($('platform').value);
    $('base_url').value = d.base_url;
    $('model_name').value = d.model;
    appendLog('已填入 ' + d.name + ' 配置', 'info');
  };

  $('btn_eye').onclick = function () {
    var k = $('api_key');
    k.type = k.type === 'password' ? 'text' : 'password';
    this.textContent = k.type === 'password' ? '👁' : '🔒';
  };

  $('saved_keys').onchange = function () {
    if (this.value) $('api_key').value = this.value;
  };
  $('btn_save_key').onclick = async function () {
    var name = $('key_name').value.trim();
    var key = $('api_key').value.trim();
    if (!name || !key) { appendLog('请输入 Key 名称与 Key 内容', 'warn'); return; }
    savedKeys = await window.pywebview.api.save_key(name, key);
    renderSavedKeys();
    $('key_name').value = '';
    appendLog('已保存 Key: ' + name, 'pass');
  };
  $('btn_del_key').onclick = async function () {
    var name = $('saved_keys').selectedOptions[0] && $('saved_keys').selectedOptions[0].textContent.split('  ·  ')[0];
    if (!name || $('saved_keys').value === '') { appendLog('请先选择要删除的 Key', 'warn'); return; }
    savedKeys = await window.pywebview.api.delete_key(name);
    renderSavedKeys();
    appendLog('已删除 Key: ' + name, 'info');
  };

  $('btn_browse').onclick = async function () {
    if (!bridgeReady) return;
    var p = await window.pywebview.api.select_file();
    if (p) {
      $('file_path').value = p;
      appendLog('已选择: ' + p.split(/[\\\\/]/).pop(), 'info');
    }
  };

  $('btn_start').onclick = async function () {
    if (running || !bridgeReady) return;
    running = true;
    setRunningUI(true);
    setBar(0);
    $('skip_box').textContent = '⛔ 不可爬取网址汇总';
    var payload = {
      platform: $('platform').value,
      base_url: $('base_url').value,
      api_key: $('api_key').value,
      model_name: $('model_name').value,
      file_path: $('file_path').value,
      urls: $('urls').value
    };
    try {
      await window.pywebview.api.submit(payload);
    } catch (e) {
      appendLog('桥接调用失败: ' + e, 'fail');
      running = false; setRunningUI(false);
    }
  };

  $('btn_pause').onclick = function () {
    if (!running) return;
    paused = !paused;
    if (paused) {
      $('btn_pause').textContent = '恢复';
      $('btn_pause').classList.add('resume');
      window.pywebview.api.pause();
    } else {
      $('btn_pause').textContent = '暂停';
      $('btn_pause').classList.remove('resume');
      window.pywebview.api.resume();
    }
  };

  $('btn_stop').onclick = function () {
    if (!running) return;
    window.pywebview.api.stop();
  };

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !running && bridgeReady) { $('btn_start').click(); }
    if (e.key === 'Escape') { $('btn_stop').click(); }
  });

  window.addEventListener('pywebviewready', function () { setTimeout(loadInitial, 150); });
</script>
</body>
</html>
"""


PLATFORMS = {
    "DeepSeek（深度求索）": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "SiliconFlow（硅基流动）": ("https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3"),
    "Moonshot（月之暗面）": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "智谱清言（GLM）": ("https://open.bigmodel.cn/api/paas/v4", "glm-4"),
    "通义千问（阿里云）": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo"),
    "OpenAI 官方": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "自定义": ("", ""),
}


class Api:
    """暴露给 JS 的 Python 桥。"""

    def __init__(self, window):
        self._window = window
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._cfg = _load_config()

    # ---- 工具 ----
    def _log(self, msg, tag="info"):
        js = "window.appendLog(%s, %s)" % (json.dumps(msg), json.dumps(tag))
        try:
            self._window.evaluate_js(js)
        except Exception:
            pass

    def _js(self, code):
        try:
            self._window.evaluate_js(code)
        except Exception:
            pass

    # ---- 初始数据 / 配置 ----
    def get_initial(self):
        return {
            "platform": self._cfg.get("platform", ""),
            "base_url": self._cfg.get("base_url", ""),
            "model": self._cfg.get("model_name", ""),
            "saved_keys": self.list_saved_keys(),
        }

    def platform_fill(self, name):
        base, model = PLATFORMS.get(name, ("", ""))
        return {"name": name, "base_url": base, "model": model}

    def save_config(self, data: dict):
        """保存当前表单到 config.json（保留已有字段）"""
        self._cfg.update({
            "platform": data.get("platform", ""),
            "base_url": data.get("base_url", ""),
            "api_key": data.get("api_key", ""),
            "model_name": data.get("model_name", ""),
        })
        _save_config(self._cfg)
        return True

    # ---- 保存的 Key 管理（config.json 持久化）----
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

    # ---- 模拟爬取任务（暂停 / 结束语义演示）----
    def submit(self, data: dict):
        self._log("已收到配置，开始模拟爬取…", "info")
        self._log("base_url: %s | model: %s | 文件: %s"
                  % (data.get("base_url"), data.get("model_name"),
                     data.get("file_path") or "（无）"), "info")
        lines = [u.strip() for u in (data.get("urls") or "").splitlines() if u.strip()]
        urls = lines or ["https://example.com/a", "https://example.com/b", "https://example.com/c"]
        self._stop.clear()
        self._pause.clear()

        def work():
            total = len(urls)
            paused_logged = False
            for i, u in enumerate(urls, 1):
                if self._stop.is_set():
                    self._log("已结束任务，跳过剩余站点", "warn")
                    break
                while self._pause.is_set():
                    if not paused_logged:
                        self._log("已暂停，等待恢复…", "warn")
                        paused_logged = True
                    time.sleep(0.4)
                paused_logged = False
                time.sleep(0.8)
                self._log("模拟处理 第%d/%d 个站点 · %s" % (i, total, u[:50]), "pass")
                self._js("window.setBar(%d); window.setStatus('正在爬取 %s')"
                         % (int(i / total * 100), json.dumps(u[:40])))
            if self._stop.is_set():
                self._log("模拟任务已结束", "warn")
            else:
                self._log("模拟完成 —— 试点壳仅验证 JS↔Python 数据通道（未真正爬取）", "pass")
            self._js("window.setBar(100); window.setStatus('完成');"
                     "window.setRunningUI(false);")
        threading.Thread(target=work, daemon=True).start()
        return True

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


def main():
    api = Api(None)
    window = webview.create_window(
        "网站爬取工具 · Linear 试点壳 v1.5",
        html=HTML,
        js_api=api,
        width=880,
        height=840,
        background_color="#0a0a0b",
        text_select=False,
    )
    api._window = window
    webview.start(gui="edgechromium")


if __name__ == "__main__":
    main()
