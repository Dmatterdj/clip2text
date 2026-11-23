/* clip2text 前端：原生 JS，无构建步骤。
   核心是四个状态：选文件 → 上传 → 轮询任务 → 展示结果。
   刻意不用框架——这个页面就三屏，引入 React 只会让 clone 变重。 */

const API = (() => {
  // 只要是由服务本身托管（任何端口），就用同源相对路径。
  // 不要写死 8000：用户可能用 --port 换端口，写死会导致能力探测全部失败。
  if (location.protocol === 'http:' || location.protocol === 'https:') {
    return '/api/v1';
  }
  // 直接双击打开 index.html（file://）时才退回本机默认端口
  return 'http://127.0.0.1:8000/api/v1';
})();

const $ = (id) => document.getElementById(id);

const state = {
  file: null,
  taskId: null,
  pollTimer: null,
  task: null,
  capabilities: null,
};

/* ---------------- 工具 ---------------- */
function toast(msg, ms = 2600) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), ms);
}

function humanSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 ** 2) return (bytes / 1024).toFixed(0) + ' KB';
  if (bytes < 1024 ** 3) return (bytes / 1024 ** 2).toFixed(1) + ' MB';
  return (bytes / 1024 ** 3).toFixed(2) + ' GB';
}

function fmtDuration(sec) {
  if (!sec) return '—';
  const t = Math.round(sec);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function fmtClock(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body.detail) detail = body.detail;
    } catch { /* 非 JSON 响应，用状态码 */ }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

/* ---------------- 能力探测 ---------------- */
async function loadCapabilities() {
  const box = $('caps');
  try {
    const caps = await api('/capabilities');
    state.capabilities = caps;

    const pills = [];
    const ff = caps.ffmpeg;
    pills.push(pill(ff.available ? `ffmpeg ${ff.source}` : 'ffmpeg 缺失', ff.available === true, ff.available === false, ff.reason));

    const asr = (caps.asr || []).filter((e) => e.available);
    if (asr.length) {
      pills.push(pill(`ASR ${asr[0].name}`, true, false));
    } else {
      pills.push(pill('无可用 ASR', false, true, '安装 faster-whisper 或 mlx-whisper'));
    }

    if (caps.has_timestamps) pills.push(pill('时间戳', true, false));
    pills.push(pill(`${caps.limits.max_upload_mb}MB 上限`, true, false));

    box.innerHTML = pills.join('');

    $('limitHint').textContent =
      `单文件最大 ${caps.limits.max_upload_mb}MB · 本机并发 ${caps.limits.max_concurrency}`;

    // 没有 ASR 就禁用主按钮并说明原因，而不是让用户点了才失败
    if (!asr.length) {
      $('startBtn').disabled = true;
      setStatus('没有可用的语音识别引擎，请先安装', 'err');
    }
  } catch (err) {
    box.innerHTML = pill('能力探测失败', false, true, err.message);
  }
}

function pill(text, ok, bad, reason) {
  const cls = ok ? 'yes' : bad ? 'no' : '';
  const title = reason ? ` title="${escapeHtml(reason)}"` : '';
  return `<span class="pill ${cls}"${title}><span class="dot"></span>${escapeHtml(text)}</span>`;
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

/* ---------------- 文件选择 ---------------- */
function setFile(file) {
  if (!file) return;
  state.file = file;
  $('fileName').textContent = file.name;
  $('fileSize').textContent = humanSize(file.size);
  $('fileRow').hidden = false;
  $('startBtn').disabled = !hasAsr();
  setStatus('');
}

function hasAsr() {
  const caps = state.capabilities;
  return !!(caps && (caps.asr || []).some((e) => e.available));
}

function clearFile() {
  state.file = null;
  state.taskId = null;
  state.task = null;
  $('fileInput').value = '';
  $('fileRow').hidden = true;
  $('uploadProgress').hidden = true;
  $('startBtn').disabled = true;
  $('resultCard').hidden = true;
  setStatus('');
  resetTabs();
}

function setStatus(text, kind) {
  const el = $('statusText');
  el.textContent = text || '';
  el.className = 'status-text' + (kind ? ' ' + kind : '');
}

/* ---------------- 上传 ---------------- */
function upload(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', API + '/uploads');

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText)); }
        catch { reject(new Error('上传响应解析失败')); }
      } else {
        let detail = `上传失败 (${xhr.status})`;
        try { detail = JSON.parse(xhr.responseText).detail || detail; } catch { /* ignore */ }
        reject(new Error(detail));
      }
    };
    xhr.onerror = () => reject(new Error('网络错误'));
    xhr.ontimeout = () => reject(new Error('上传超时'));

    const fd = new FormData();
    fd.append('file', file, file.name);
    xhr.send(fd);
  });
}

/* ---------------- 任务轮询 ---------------- */
const STAGE_LABEL = {
  queued: '排队中',
  fetching: '读取文件',
  extracting: '解析链接',
  transcoding: '抽取音频',
  transcribing: '语音转写',
  ocr: '图片识别',
  packaging: '整理结果',
  done: '完成',
};

function startPolling(taskId) {
  stopPolling();
  const tick = async () => {
    try {
      const task = await api(`/tasks/${taskId}`);
      state.task = task;
      renderProgress(task);
      if (task.status === 'SUCCESS') {
        stopPolling();
        setStatus('完成', 'ok');
        renderResult(task);
        loadHistory();
      } else if (task.status === 'FAILED' || task.status === 'CANCELED') {
        stopPolling();
        setStatus(task.error_message || '任务失败', 'err');
        $('cancelBtn').hidden = true;
        $('startBtn').disabled = false;
      }
    } catch (err) {
      stopPolling();
      setStatus(err.message, 'err');
      $('startBtn').disabled = false;
    }
  };
  tick();
  // 3 秒轮询：任务通常 10-60 秒完成，SSE 也可以，但轮询对本地单用户更稳
  state.pollTimer = setInterval(tick, 3000);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function renderProgress(task) {
  const stage = STAGE_LABEL[task.stage] || task.stage;
  if (task.status === 'RUNNING' || task.status === 'QUEUED') {
    setStatus(`${stage} · ${task.progress}%`);
  }
}

/* ---------------- 开始 ---------------- */
async function start() {
  if (!state.file) return;
  const btn = $('startBtn');
  btn.disabled = true;
  $('cancelBtn').hidden = true;
  $('resultCard').hidden = true;

  try {
    // 1. 上传
    $('uploadProgress').hidden = false;
    $('uploadBar').style.width = '0%';
    $('uploadPct').textContent = '0%';
    setStatus('上传中…');

    const up = await upload(state.file, (pct) => {
      $('uploadBar').style.width = pct + '%';
      $('uploadPct').textContent = pct + '%';
    });

    $('uploadProgress').hidden = true;
    setStatus('已上传，排队中…');

    // 2. 创建任务
    const task = await api('/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_type: 'upload',
        source_value: up.path,
        title: state.file.name.replace(/\.[^.]+$/, ''),
        work_type: up.work_type,
      }),
    });

    state.taskId = task.id;
    $('cancelBtn').hidden = false;
    startPolling(task.id);
  } catch (err) {
    setStatus(err.message, 'err');
    btn.disabled = false;
    $('uploadProgress').hidden = true;
  }
}

async function cancel() {
  if (!state.taskId) return;
  try {
    await api(`/tasks/${state.taskId}/cancel`, { method: 'POST' });
    stopPolling();
    setStatus('已取消');
  } catch (err) {
    toast(err.message);
  } finally {
    $('cancelBtn').hidden = true;
    $('startBtn').disabled = false;
  }
}

/* ---------------- 结果渲染 ---------------- */
function renderResult(task) {
  $('resultCard').hidden = false;
  $('startBtn').disabled = false;
  $('cancelBtn').hidden = true;

  const meta = [];
  if (task.duration) meta.push(`时长 <b>${fmtDuration(task.duration)}</b>`);
  if (task.text_content) meta.push(`字数 <b>${task.text_content.length}</b>`);
  if (task.segments && task.segments.length) meta.push(`分句 <b>${task.segments.length}</b>`);
  if (task.language) meta.push(`语言 <b>${escapeHtml(task.language)}</b>`);
  if (task.engine) meta.push(`引擎 <b>${escapeHtml(task.engine)}</b>`);
  $('resultMeta').innerHTML = meta.join('') || '—';

  $('resultText').value = task.text_content || '';

  const segBox = $('segments');
  if (task.segments && task.segments.length) {
    segBox.innerHTML = task.segments.map((s) => `
      <div class="seg">
        <time>${fmtClock(s.start)}</time>
        <span>${escapeHtml(s.text)}</span>
      </div>`).join('');
  } else {
    segBox.innerHTML = '<div class="seg"><time>—</time><span>当前引擎不返回带时间戳的分句</span></div>';
  }

  resetTabs();
  $('resultCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function resetTabs() {
  document.querySelectorAll('.tab').forEach((t) => {
    const on = t.dataset.tab === 'text';
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', String(on));
  });
  $('panel-text').hidden = false;
  $('panel-segments').hidden = true;
}

/* ---------------- 导出 ---------------- */
async function doExport(format) {
  if (!state.taskId) {
    toast('没有可导出的任务');
    return;
  }
  try {
    const res = await fetch(`${API}/tasks/${state.taskId}/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ format }),
    });
    if (!res.ok) {
      let detail = `导出失败 (${res.status})`;
      try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
      throw new Error(detail);
    }
    const blob = await res.blob();

    // 从 Content-Disposition 取文件名，取不到就自己拼
    let filename = `clip2text-${state.taskId.slice(0, 8)}.${format === 'markdown' ? 'md' : format}`;
    const cd = res.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*=UTF-8''([^;]+)/i);
    if (m) {
      try { filename = decodeURIComponent(m[1]); } catch { /* 保留默认名 */ }
    }

    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`已导出 ${filename}`);
  } catch (err) {
    toast(err.message, 3600);
  }
}

/* ---------------- 历史 ---------------- */
async function loadHistory() {
  const box = $('history');
  try {
    const data = await api('/tasks?limit=20');
    if (!data.items.length) {
      box.innerHTML = '<div class="hist-empty">还没有记录</div>';
      return;
    }
    box.innerHTML = data.items.map((t) => {
      const name = t.title || t.source_value.split('/').pop() || t.id.slice(0, 8);
      const when = new Date(t.created_at).toLocaleString('zh-CN', {
        month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
      });
      return `<div class="hist-item" data-id="${t.id}">
        <span class="badge ${t.status}">${statusLabel(t.status)}</span>
        <span class="t">${escapeHtml(name)}</span>
        <span class="d">${when}</span>
      </div>`;
    }).join('');

    box.querySelectorAll('.hist-item').forEach((el) => {
      el.addEventListener('click', () => openHistoryTask(el.dataset.id));
    });
  } catch (err) {
    box.innerHTML = `<div class="hist-empty">加载失败：${escapeHtml(err.message)}</div>`;
  }
}

function statusLabel(s) {
  return { SUCCESS: '完成', FAILED: '失败', RUNNING: '进行中', QUEUED: '排队', CANCELED: '已取消' }[s] || s;
}

async function openHistoryTask(id) {
  try {
    const task = await api(`/tasks/${id}`);
    state.taskId = task.id;
    state.task = task;
    if (task.status === 'SUCCESS') {
      renderResult(task);
    } else if (task.status === 'RUNNING' || task.status === 'QUEUED') {
      setStatus(`${STAGE_LABEL[task.stage] || task.stage} · ${task.progress}%`);
      startPolling(task.id);
    } else {
      toast(task.error_message || statusLabel(task.status), 3600);
    }
  } catch (err) {
    toast(err.message);
  }
}

/* ---------------- 事件绑定 ---------------- */
function bind() {
  const dz = $('dropzone');
  const input = $('fileInput');

  dz.addEventListener('click', () => input.click());
  dz.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
  });

  ['dragenter', 'dragover'].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.add('dragging');
    }));
  ['dragleave', 'drop'].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.remove('dragging');
    }));

  dz.addEventListener('drop', (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (f) setFile(f);
  });

  input.addEventListener('change', () => {
    if (input.files?.[0]) setFile(input.files[0]);
  });

  $('clearFile').addEventListener('click', (e) => {
    e.stopPropagation();
    clearFile();
  });

  $('startBtn').addEventListener('click', start);
  $('cancelBtn').addEventListener('click', cancel);
  $('refreshHistory').addEventListener('click', loadHistory);

  $('copyText').addEventListener('click', async () => {
    const text = $('resultText').value;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      const hint = $('copyHint');
      hint.textContent = '已复制';
      setTimeout(() => { hint.textContent = ''; }, 1800);
    } catch {
      $('resultText').select();
      toast('请按 ⌘C / Ctrl+C 复制');
    }
  });

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((t) => {
        const on = t === tab;
        t.classList.toggle('active', on);
        t.setAttribute('aria-selected', String(on));
      });
      $('panel-text').hidden = tab.dataset.tab !== 'text';
      $('panel-segments').hidden = tab.dataset.tab !== 'segments';
    });
  });

  document.querySelectorAll('[data-format]').forEach((btn) => {
    btn.addEventListener('click', () => doExport(btn.dataset.format));
  });
}

/* ---------------- 启动 ---------------- */
bind();
loadCapabilities();
loadHistory();
