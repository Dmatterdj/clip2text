/* clip2text 前端：原生 JS，无构建步骤。
   四种交互：单文件转写、批量链接、结果导出、AI 改写。
   刻意不用框架——这个页面就几屏，引入 React 只会让 clone 变重。 */

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
  mode: 'single',
  file: null,
  taskId: null,
  pollTimer: null,
  task: null,
  capabilities: null,
  batchId: null,
  batchTimer: null,
  batchTasks: [],
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

const fmtClock = (sec) => {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
};

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
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
    pills.push(pill(ff.available ? `ffmpeg ${ff.source}` : 'ffmpeg 缺失',
      ff.available, !ff.available, ff.reason));

    const asr = (caps.asr || []).filter((e) => e.available);
    pills.push(asr.length
      ? pill(`ASR ${asr[0].name}`, true, false)
      : pill('无可用 ASR', false, true, '安装 faster-whisper 或 mlx-whisper'));

    const ocr = (caps.ocr || []).filter((e) => e.available);
    if (ocr.length) pills.push(pill(`OCR ${ocr[0].name}`, true, false));

    if (caps.has_timestamps) pills.push(pill('时间戳', true, false));
    pills.push(pill(`${caps.limits.max_upload_mb}MB 上限`, true, false));

    box.innerHTML = pills.join('');

    $('limitHint').textContent =
      `音视频或图片 · 单文件最大 ${caps.limits.max_upload_mb}MB`;
    $('batchLimit').textContent = caps.limits.max_batch_size;

    // 没有 ASR 就禁用主按钮并说明原因，而不是让用户点了才失败
    if (!asr.length) {
      $('startBtn').disabled = true;
      setStatus('没有可用的语音识别引擎，请先安装', 'err');
    }

    // 改写入口：没配 LLM 就整个隐藏
    if (caps.rewrite && caps.rewrite.available) {
      $('rewriteTab').hidden = false;
      await loadStyles();
    }

    // 链接解析关闭时，批量模式给出明确指引
    if (!caps.extractors.enabled) {
      $('extractWarn').hidden = false;
      $('batchStart').disabled = true;
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

const hasAsr = () =>
  !!(state.capabilities && (state.capabilities.asr || []).some((e) => e.available));

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

/* ---------------- 单任务轮询 ---------------- */
const STAGE_LABEL = {
  queued: '排队中', fetching: '读取文件', extracting: '解析链接',
  transcoding: '抽取音频', transcribing: '语音转写', ocr: '图片识别',
  packaging: '整理结果', done: '完成',
};

function startPolling(taskId) {
  stopPolling();
  const tick = async () => {
    try {
      const task = await api(`/tasks/${taskId}`);
      state.task = task;
      if (task.status === 'RUNNING' || task.status === 'QUEUED') {
        const stage = STAGE_LABEL[task.stage] || task.stage;
        setStatus(`${stage} · ${task.progress}%`);
      }
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
  // 3 秒轮询：任务通常 10-60 秒完成，对本地单用户比 SSE 更稳
  state.pollTimer = setInterval(tick, 3000);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

/* ---------------- 开始单任务 ---------------- */
async function start() {
  if (!state.file) return;
  const btn = $('startBtn');
  btn.disabled = true;
  $('cancelBtn').hidden = true;
  $('resultCard').hidden = true;

  try {
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

  // 换任务时清掉上一轮的改写结果，避免张冠李戴
  $('rewriteOutput').value = '';
  $('rewriteStatus').textContent = '';

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
  const rw = $('panel-rewrite');
  if (rw) rw.hidden = true;
}

/* ---------------- 导出 ---------------- */
function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function filenameFrom(res, fallback) {
  const cd = res.headers.get('Content-Disposition') || '';
  const m = cd.match(/filename\*=UTF-8''([^;]+)/i);
  if (m) {
    try { return decodeURIComponent(m[1]); } catch { /* 用默认名 */ }
  }
  return fallback;
}

async function doExport(format) {
  if (!state.taskId) { toast('没有可导出的任务'); return; }
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
    const ext = format === 'markdown' ? 'md' : format;
    const fallback = `clip2text-${state.taskId.slice(0, 8)}.${ext}`;
    const filename = filenameFrom(res, fallback);
    triggerDownload(blob, filename);
    toast(`已导出 ${filename}`);
  } catch (err) {
    toast(err.message, 3600);
  }
}

/* ---------------- 改写 ---------------- */
async function loadStyles() {
  try {
    const data = await api('/rewrite/styles');
    const sel = $('styleSelect');
    sel.innerHTML = data.styles
      .map((s) => `<option value="${s.key}" title="${escapeHtml(s.description)}">${escapeHtml(s.label)}</option>`)
      .join('');
    sel.value = data.default || 'summary';
  } catch (err) {
    console.warn('加载改写风格失败', err);
  }
}

async function doRewrite() {
  if (!state.taskId) { toast('没有可改写的任务'); return; }
  const style = $('styleSelect').value;
  const out = $('rewriteOutput');
  const hint = $('rewriteStatus');
  out.value = '';
  hint.textContent = '改写中…';
  hint.className = 'status-text';

  try {
    const res = await fetch(`${API}/rewrite`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: state.taskId, style }),
    });
    if (!res.ok) {
      let detail = `请求失败 (${res.status})`;
      try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
      throw new Error(detail);
    }

    // 手写 SSE 解析：EventSource 只支持 GET，这里需要 POST
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const blocks = buf.split('\n\n');
      buf = blocks.pop() || '';
      for (const block of blocks) {
        const evMatch = block.match(/^event:\s*(\w+)/m);
        const dataMatch = block.match(/^data:\s*(.+)$/m);
        if (!dataMatch) continue;
        let payload;
        try { payload = JSON.parse(dataMatch[1]); } catch { continue; }
        const ev = evMatch ? evMatch[1] : 'message';
        if (ev === 'delta' && payload.text) {
          out.value += payload.text;
          out.scrollTop = out.scrollHeight;
        } else if (ev === 'error') {
          throw new Error(payload.detail || '改写失败');
        } else if (ev === 'done') {
          hint.textContent = '完成';
          hint.className = 'status-text ok';
        }
      }
    }
    if (hint.textContent === '改写中…') {
      hint.textContent = '完成';
      hint.className = 'status-text ok';
    }
  } catch (err) {
    hint.textContent = err.message;
    hint.className = 'status-text err';
  }
}

/* ---------------- 批量 ---------------- */
async function startBatch() {
  const text = $('batchInput').value.trim();
  if (!text) { toast('请先粘贴链接'); return; }

  const btn = $('batchStart');
  btn.disabled = true;
  $('batchCancel').hidden = false;
  $('batchResultCard').hidden = true;
  setBatchStatus('提交中…');

  try {
    const batch = await api('/batches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_type: 'url', text }),
    });

    state.batchId = batch.id;
    $('batchProgress').hidden = false;
    $('batchBar').style.width = '0%';
    $('batchPct').textContent = '0%';

    const notes = [];
    if (batch.duplicates.length) notes.push(`${batch.duplicates.length} 条重复已跳过`);
    if (batch.invalid.length) notes.push(`${batch.invalid.length} 条无效已跳过`);
    setBatchStatus(`已提交 ${batch.total} 条${notes.length ? '（' + notes.join('，') + '）' : ''}`);

    startBatchPolling(batch.id);
  } catch (err) {
    setBatchStatus(err.message, 'err');
    btn.disabled = false;
    $('batchCancel').hidden = true;
  }
}

function setBatchStatus(text, kind) {
  const el = $('batchStatus');
  el.textContent = text || '';
  el.className = 'status-text' + (kind ? ' ' + kind : '');
}

function startBatchPolling(batchId) {
  stopBatchPolling();
  const tick = async () => {
    try {
      const data = await api(`/batches/${batchId}`);
      const b = data.batch;
      state.batchTasks = data.tasks;

      $('batchBar').style.width = `${data.progress}%`;
      $('batchPct').textContent = `${data.progress}%`;
      setBatchStatus(`${b.succeeded} 成功 / ${b.failed} 失败 / 共 ${b.total}`);

      renderBatchList(data.tasks);

      if (['DONE', 'PARTIAL', 'FAILED', 'CANCELED'].includes(b.status)) {
        stopBatchPolling();
        $('batchStart').disabled = !state.capabilities?.extractors?.enabled;
        $('batchCancel').hidden = true;
        $('batchResultCard').hidden = false;
        $('batchSummary').textContent =
          { DONE: '全部完成', PARTIAL: '部分完成', FAILED: '全部失败', CANCELED: '已取消' }[b.status] || b.status;
        setBatchStatus($('batchSummary').textContent,
          b.status === 'DONE' ? 'ok' : b.status === 'FAILED' ? 'err' : '');
        loadHistory();
      }
    } catch (err) {
      stopBatchPolling();
      setBatchStatus(err.message, 'err');
      $('batchStart').disabled = false;
    }
  };
  tick();
  state.batchTimer = setInterval(tick, 2500);
}

function stopBatchPolling() {
  if (state.batchTimer) {
    clearInterval(state.batchTimer);
    state.batchTimer = null;
  }
}

function renderBatchList(tasks) {
  const box = $('batchList');
  if (!tasks.length) { box.innerHTML = ''; return; }

  box.innerHTML = tasks.map((t) => {
    const name = t.title || t.source_value;
    const sub = t.status === 'SUCCESS'
      ? `${(t.text_content || '').length} 字`
      : t.status === 'FAILED'
        ? (t.error_message || '失败')
        : `${STAGE_LABEL[t.stage] || t.stage} · ${t.progress}%`;
    const cls = t.status === 'SUCCESS' ? 'done' : '';
    const exportable = t.status === 'SUCCESS';
    return `<div class="batch-item ${cls}" data-task="${t.id}" data-ok="${exportable}">
      <span class="badge ${t.status}">${statusLabel(t.status)}</span>
      <span class="bi-main">
        <span class="bi-title">${escapeHtml(name)}</span>
        <span class="bi-sub">${escapeHtml(sub)}</span>
      </span>
      <span class="bi-actions">
        ${exportable ? `<button class="btn ghost small" data-export-task="${t.id}" data-format="txt">TXT</button>` : ''}
      </span>
    </div>`;
  }).join('');

  // 点条目打开详情
  box.querySelectorAll('.batch-item.done').forEach((el) => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('[data-export-task]')) return;
      openHistoryTask(el.dataset.task);
    });
  });
  // 单条导出
  box.querySelectorAll('[data-export-task]').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const prev = state.taskId;
      state.taskId = btn.dataset.exportTask;
      await doExport(btn.dataset.format);
      state.taskId = prev;
    });
  });
}

async function cancelBatch() {
  if (!state.batchId) return;
  try {
    await api(`/batches/${state.batchId}/cancel`, { method: 'POST' });
    setBatchStatus('取消中…');
  } catch (err) {
    toast(err.message);
  }
}

/* 合并导出：把多个成功任务串成一份 md / txt */
async function batchExport(format) {
  const ok = state.batchTasks.filter((t) => t.status === 'SUCCESS');
  if (!ok.length) { toast('没有成功的条目可导出'); return; }

  const parts = [];
  if (format === 'txt') {
    for (const t of ok) {
      parts.push('='.repeat(50));
      parts.push(t.title || t.source_value);
      parts.push('='.repeat(50));
      parts.push('');
      parts.push(t.text_content || '');
      parts.push('\n');
    }
  } else {
    parts.push(`# 批量导出（${ok.length} 条）\n`);
    for (const t of ok) {
      parts.push(`## ${t.title || t.source_value}\n`);
      if (t.duration) parts.push(`- 时长：${fmtDuration(t.duration)}`);
      parts.push(`- 字数：${(t.text_content || '').length}\n`);
      parts.push(t.text_content || '');
      parts.push('');
    }
  }

  const blob = new Blob([parts.join('\n')], { type: 'text/plain;charset=utf-8' });
  triggerDownload(blob, `clip2text-batch.${format === 'markdown' ? 'md' : 'txt'}`);
  toast(`已导出 ${ok.length} 条`);
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
  return {
    SUCCESS: '完成', FAILED: '失败', RUNNING: '进行中',
    QUEUED: '排队', CANCELED: '已取消',
  }[s] || s;
}

async function openHistoryTask(id) {
  try {
    // 打开历史任务时确保回到单文件模式
    if (state.mode !== 'single') switchMode('single');
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

/* ---------------- 模式切换 ---------------- */
function switchMode(mode) {
  state.mode = mode;
  document.querySelectorAll('.mode').forEach((m) => {
    const on = m.dataset.mode === mode;
    m.classList.toggle('active', on);
    m.setAttribute('aria-selected', String(on));
  });
  $('mode-single').hidden = mode !== 'single';
  $('mode-batch').hidden = mode !== 'batch';
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
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('dragging'); }));
  ['dragleave', 'drop'].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('dragging'); }));
  dz.addEventListener('drop', (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (f) setFile(f);
  });
  input.addEventListener('change', () => {
    if (input.files?.[0]) setFile(input.files[0]);
  });

  $('clearFile').addEventListener('click', (e) => { e.stopPropagation(); clearFile(); });
  $('startBtn').addEventListener('click', start);
  $('cancelBtn').addEventListener('click', cancel);
  $('refreshHistory').addEventListener('click', loadHistory);

  $('batchStart').addEventListener('click', startBatch);
  $('batchCancel').addEventListener('click', cancelBatch);

  document.querySelectorAll('.mode').forEach((m) => {
    m.addEventListener('click', () => switchMode(m.dataset.mode));
  });

  $('copyText').addEventListener('click', () => copyFrom($('resultText'), $('copyHint')));
  $('copyRewrite').addEventListener('click', () => copyFrom($('rewriteOutput'), $('rewriteHint')));
  $('doRewrite').addEventListener('click', doRewrite);

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((t) => {
        const on = t === tab;
        t.classList.toggle('active', on);
        t.setAttribute('aria-selected', String(on));
      });
      $('panel-text').hidden = tab.dataset.tab !== 'text';
      $('panel-segments').hidden = tab.dataset.tab !== 'segments';
      $('panel-rewrite').hidden = tab.dataset.tab !== 'rewrite';
    });
  });

  document.querySelectorAll('[data-format]').forEach((btn) => {
    if (btn.dataset.batchExport) return;
    btn.addEventListener('click', () => doExport(btn.dataset.format));
  });
  document.querySelectorAll('[data-batch-export]').forEach((btn) => {
    btn.addEventListener('click', () => batchExport(btn.dataset.batchExport));
  });
}

async function copyFrom(el, hintEl) {
  const text = el.value;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    hintEl.textContent = '已复制';
    setTimeout(() => { hintEl.textContent = ''; }, 1800);
  } catch {
    el.select();
    toast('请按 ⌘C / Ctrl+C 复制');
  }
}

/* ---------------- 启动 ---------------- */
bind();
loadCapabilities();
loadHistory();
