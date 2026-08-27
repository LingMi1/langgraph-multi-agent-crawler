/* ============================================================================
   任务工作台 · 校招版 — 前端控制器
   能力：schema 动态表单 / 双主题切换 / 拖拽导入 / 日志虚拟滚动 / toast·空态·骨架
        / 实时统计卡 / 业务切换（验证可替换架构）
   ============================================================================ */

var MAX_LOGS = 400;          // 日志虚拟滚动：最多保留 N 条 DOM
var MAX_SUMMARY = 150;

var state = {
  runner: null,
  schema: null,
  running: false,
  paused: false,
  bridgeReady: false,
  theme: localStorage.getItem('theme') || 'dark',
  logCount: 0,
  summaryCount: 0,
  lastInputs: {}
};

function $(id) { return document.getElementById(id); }

/* ============================ 主题切换 ============================ */
function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
  $('btn_theme').textContent = state.theme === 'dark' ? '☀' : '☾';
  $('btn_theme').title = state.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题';
  localStorage.setItem('theme', state.theme);
}
$('btn_theme').onclick = function () {
  state.theme = state.theme === 'dark' ? 'light' : 'dark';
  applyTheme();
  toast('已切换为' + (state.theme === 'dark' ? '深色' : '浅色') + '主题');
};
applyTheme();

/* ============================ toast ============================ */
function toast(msg, type) {
  var box = $('toasts');
  while (box.children.length >= 4) box.removeChild(box.firstChild);
  var el = document.createElement('div');
  el.className = 'toast ' + (type || '');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(function () { el.remove(); }, 2600);
}

/* ============================ 日志 / 汇总 / 统计 ============================ */
function appendLog(msg, tag) {
  var box = $('log');
  if ($('log_empty')) $('log_empty').remove();
  var t = new Date().toTimeString().slice(0, 8);
  var div = document.createElement('div');
  div.className = tag || 'info';
  div.innerHTML = '<span class="ts">[' + t + ']</span> ' + msg;
  box.appendChild(div);
  if (box.children.length > MAX_LOGS) box.removeChild(box.firstChild); // 虚拟滚动
  state.logCount++;
  $('log_count').textContent = '· ' + state.logCount;
  box.scrollTop = box.scrollHeight;
}
function appendSummary(msg) {
  var box = $('summary');
  if ($('summary_empty')) $('summary_empty').remove();
  var div = document.createElement('div');
  div.className = 'skip';
  div.textContent = msg;
  box.appendChild(div);
  if (box.children.length > MAX_SUMMARY) box.removeChild(box.firstChild);
  state.summaryCount++;
  box.scrollTop = box.scrollHeight;
}
function setStats(s) {
  $('stat_total').textContent = s.total != null ? s.total : '–';
  $('stat_done').textContent = s.done != null ? s.done : '–';
  $('stat_failed').textContent = s.failed != null ? s.failed : '–';
  $('stat_queue').textContent = s.queue != null ? s.queue : '–';
  $('stat_percent').textContent = (s.percent != null && s.percent >= 0) ? s.percent + '%' : '–';
}
function setStatus(s) { $('status').textContent = s; }
function setBar(pct) { $('bar_fill').classList.remove('busy'); $('bar_fill').style.width = pct + '%'; }
function setBusy(b) { $('bar_fill').classList.toggle('busy', b); }
function resetOutput() {
  var log = $('log'); log.innerHTML = '<div class="empty" id="log_empty">暂无日志 —— 点击「开始任务」运行</div>';
  var sum = $('summary'); sum.innerHTML = '<div class="empty" id="summary_empty">运行后显示汇总 / 输出</div>';
  state.logCount = 0; state.summaryCount = 0;
  $('log_count').textContent = '';
  setStats({});
  setBar(0);
}
function setRunningUI(on) {
  $('btn_start').disabled = on;
  $('btn_pause').disabled = !on;
  $('btn_stop').disabled = !on;
  if (!on) {
    state.paused = false;
    $('btn_pause').textContent = '暂停';
    $('btn_pause').classList.remove('resume');
  }
}

/* ============================ 动态表单 ============================ */
function fieldHtml(f) {
  var id = 'f_' + f.key;
  var val = (state.lastInputs[state.runner] && state.lastInputs[state.runner][f.key] != null)
    ? state.lastInputs[state.runner][f.key] : (f.default || '');
  var esc = function (s) { return String(s).replace(/"/g, '&quot;'); };
  var opt = (f.options || []).map(function (o) {
    return '<option' + (o === val ? ' selected' : '') + '>' + esc(o) + '</option>';
  }).join('');

  var inner;
  if (f.type === 'textarea') {
    inner = '<textarea class="field" id="' + id + '" placeholder="' + esc(f.placeholder) + '"></textarea>';
  } else if (f.type === 'select') {
    inner = '<select class="field" id="' + id + '"><option value="">— 请选择 —</option>' + opt + '</select>';
  } else if (f.type === 'file') {
    inner = '<div class="inline">' +
      '<input class="field grow" id="' + id + '" type="text" readonly placeholder="' + esc(f.placeholder) + '">' +
      '<button class="btn-ghost" data-browse="' + id + '">浏览…</button></div>';
  } else if (f.type === 'password') {
    inner = '<div class="inline">' +
      '<input class="field grow" id="' + id + '" type="password" placeholder="' + esc(f.placeholder) + '">' +
      '<button class="btn-ghost" data-eye="' + id + '">👁</button></div>';
  } else {
    inner = '<input class="field" id="' + id + '" type="' + (f.type === 'number' ? 'number' : 'text') + '" placeholder="' + esc(f.placeholder) + '">';
  }
  return '<div class="row"><label>' + esc(f.label) + (f.required ? ' <span style="color:var(--error)">*</span>' : '') + '</label>' + inner + '</div>';
}

function renderForm(schema) {
  var box = $('form');
  box.innerHTML = schema.fields.map(fieldHtml).join('');

  // 平台联动（爬虫业务）
  var pf = $('f_platform');
  if (pf) pf.addEventListener('change', async function () {
    if (!state.bridgeReady) return;
    try {
      var d = await window.pywebview.api.platform_fill(pf.value);
      if (d.base_url && $('f_base_url')) $('f_base_url').value = d.base_url;
      if (d.model && $('f_model_name')) $('f_model_name').value = d.model;
      toast('已填入 ' + d.name + ' 配置');
    } catch (e) { /* 桥接未就绪 */ }
  });

  // 浏览文件
  box.querySelectorAll('[data-browse]').forEach(function (b) {
    b.onclick = async function () {
      if (!state.bridgeReady) return;
      var p = await window.pywebview.api.select_file();
      if (p) { $('f_' + b.dataset.browse.slice(2)).value = p; toast('已选择: ' + p.split(/[\\/]/).pop()); }
    };
  });
  // Key 显隐
  box.querySelectorAll('[data-eye]').forEach(function (b) {
    b.onclick = function () {
      var k = $('f_' + b.dataset.eye.slice(2));
      k.type = k.type === 'password' ? 'text' : 'password';
      b.textContent = k.type === 'password' ? '👁' : '🔒';
    };
  });

  // Key 管理区：仅当 schema 含 password 字段时显示
  $('keys_box').hidden = !schema.fields.some(function (f) { return f.type === 'password'; });
}

/* ============================ 业务切换 ============================ */
function switchRunner(name, data) {
  state.runner = name;
  state.schema = data.schema;
  $('env_badge').textContent = name;
  renderForm(state.schema);
  resetOutput();
  setStatus('业务：' + name + ' —— 配置参数后点击「开始任务」');
}

$('runner_sel').onchange = async function () {
  if (!state.bridgeReady) return;
  var name = this.value;
  var d = await window.pywebview.api.get_schema(name);
  switchRunner(name, d);
  toast('已切换到业务：' + name);
};

/* ============================ 拖拽导入 ============================ */
(function () {
  var mask = document.createElement('div');
  mask.className = 'drop-mask';
  mask.innerHTML = '<span>松开以导入文件</span>';
  document.body.appendChild(mask);
  var dragDepth = 0;

  window.addEventListener('dragenter', function (e) {
    e.preventDefault();
    var hasFile = state.schema && state.schema.fields.some(function (f) { return f.type === 'file'; });
    if (!hasFile) return;
    dragDepth++;
    mask.classList.add('on');
  });
  window.addEventListener('dragover', function (e) { e.preventDefault(); });
  window.addEventListener('dragleave', function (e) {
    e.preventDefault();
    if (--dragDepth <= 0) { dragDepth = 0; mask.classList.remove('on'); }
  });
  window.addEventListener('drop', function (e) {
    e.preventDefault();
    dragDepth = 0;
    mask.classList.remove('on');
    var hasFile = state.schema && state.schema.fields.some(function (f) { return f.type === 'file'; });
    if (!hasFile) { toast('当前业务不支持文件导入', 'warn'); return; }
    var file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (!file) { toast('未检测到文件', 'warn'); return; }
    var path = file.path || '';
    if (path) {
      var fk = state.schema.fields.filter(function (f) { return f.type === 'file'; })[0].key;
      $('f_' + fk).value = path;
      toast('已拖入: ' + file.name, 'pass');
    } else {
      toast('拖放路径不可用，请使用「浏览…」按钮', 'warn');
    }
  });
})();

/* ============================ 桥接就绪 / 初始加载 ============================ */
function toggleReady() {
  if (window.pywebview && window.pywebview.api) {
    state.bridgeReady = true;
    loadInitial();
    appendLog('pywebview 桥接就绪', 'pass');
    toast('桥接就绪，可以开始');
  }
}
window.addEventListener('pywebviewready', toggleReady);
setTimeout(toggleReady, 800);

async function loadInitial() {
  var d = await window.pywebview.api.get_initial();
  state.lastInputs = d.last_inputs || {};

  // 业务列表 + 默认业务
  var sel = $('runner_sel');
  sel.innerHTML = d.runners.map(function (r) { return '<option>' + r + '</option>'; }).join('');
  var first = (d.runners || [])[0];
  sel.value = first;
  switchRunner(first, { schema: d.schema });

  // 保存的 Key
  window.__savedKeys = d.saved_keys || [];
  renderSavedKeys();
  appendLog('已加载配置，保存的 Key ' + (window.__savedKeys.length) + ' 个');
}

function renderSavedKeys() {
  var sel = $('saved_keys');
  sel.innerHTML = '<option value="">— 选择已保存 —</option>';
  (window.__savedKeys || []).forEach(function (k) {
    var o = document.createElement('option');
    o.value = k.key;
    o.textContent = k.name + ' · ' + k.key.slice(0, 10) + '…';
    sel.appendChild(o);
  });
}
$('saved_keys').onchange = function () {
  if (this.value) { $('f_api_key').value = this.value; toast('已填入 Key'); }
};
$('btn_save_key').onclick = async function () {
  var name = $('key_name').value.trim();
  var key = ($('f_api_key') && $('f_api_key').value.trim()) || '';
  if (!name || !key) { toast('请输入 Key 名称与内容', 'warn'); return; }
  window.__savedKeys = await window.pywebview.api.save_key(name, key);
  renderSavedKeys();
  $('key_name').value = '';
  toast('已保存 Key: ' + name, 'pass');
};
$('btn_del_key').onclick = async function () {
  var sel = $('saved_keys');
  var text = sel.selectedOptions[0] && sel.selectedOptions[0].textContent;
  var name = text && text.split(' · ')[0];
  if (!name || sel.value === '') { toast('请先选择要删除的 Key', 'warn'); return; }
  window.__savedKeys = await window.pywebview.api.delete_key(name);
  renderSavedKeys();
  toast('已删除 Key: ' + name);
};

/* ============================ 运行控制 ============================ */
function collectPayload() {
  var payload = { _runner: state.runner };
  state.schema.fields.forEach(function (f) {
    payload[f.key] = $('f_' + f.key).value;
  });
  return payload;
}

$('btn_start').onclick = async function () {
  if (state.running || !state.bridgeReady) return;
  var payload = collectPayload();
  // 必填校验
  var missing = state.schema.fields.filter(function (f) {
    return f.required && !String(payload[f.key] || '').trim();
  });
  if (missing.length) { toast('请填写必填项：' + missing[0].label, 'warn'); return; }

  state.running = true;
  setRunningUI(true);
  resetOutput();
  setBusy(true);
  setStatus('启动中…');
  try {
    await window.pywebview.api.submit(payload);
  } catch (e) {
    toast('调用失败: ' + e, 'fail');
    state.running = false;
    setRunningUI(false);
    setBusy(false);
  }
};

$('btn_pause').onclick = function () {
  if (!state.running) return;
  state.paused = !state.paused;
  if (state.paused) {
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
  if (!state.running) return;
  window.pywebview.api.stop();
};

document.addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !state.running && state.bridgeReady) $('btn_start').click();
  if (e.key === 'Escape') $('btn_stop').click();
});

/* ============ 桥接推入（Python → JS）全局钩子 ============ */
window.appendLog = appendLog;
window.appendSummary = appendSummary;
window.setStats = setStats;
window.setStatus = setStatus;
window.setBar = setBar;
window.setBusy = setBusy;
window.setRunningUI = setRunningUI;
window.resetOutput = resetOutput;
