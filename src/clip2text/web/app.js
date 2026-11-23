/* ============================================================================
 * clip2text 前端
 *
 * 约束：原生 JS，无构建、无依赖、无 CDN。三个静态文件 clone 下来就能跑。
 * 主入口是「粘贴链接」；上传文件是次要入口，藏在粘贴框的下方。
 *
 * 结构：
 *   1. 工具函数     2. 状态     3. 主题     4. 能力探测
 *   5. 输入与提交   6. 任务进度  7. 结果渲染  8. 搜索
 *   9. 改写         10. 批量     11. 历史     12. 命令面板
 * ========================================================================== */

'use strict';

/* ------------------------------------------------------------------ 1. 工具 */

/* 同一个服务托管时用相对路径。写死端口会在 --port 换端口时让能力探测全挂。 */
const API = (location.protocol === 'http:' || location.protocol === 'https:')
  ? '/api/v1'
  : 'http://127.0.0.1:8000/api/v1';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* 每个分句列表一次插入多少行。500+ 条时靠这个 + content-visibility 保证不卡。 */
const SEG_CHUNK = 120;
/* 搜索态下一次渲染更多行，否则「下一处」跳不到还没渲染的行。 */
const SEG_CHUNK_SEARCH = 600;

const STEPS = [
  { key: 'queued', label: '排队' },
  { key: 'parse', label: '解析链接' },
  { key: 'audio', label: '抽取音频' },
  { key: 'asr', label: '语音转写' },
  { key: 'pack', label: '整理' },
];

/* 后端 stage -> 步骤序号 */
const STAGE_STEP = {
  queued: 0,
  fetching: 1,
  extracting: 1,
  transcoding: 2,
  transcribing: 3,
  ocr: 3,
  packaging: 4,
  done: 4,
};

const STATUS_LABEL = {
  QUEUED: '排队',
  RUNNING: '进行中',
  SUCCESS: '完成',
  FAILED: '失败',
  CANCELED: '已取消',
  PENDING: '等待',
  DONE: '全部完成',
  PARTIAL: '部分完成',
};

const STAGE_LABEL = {
  queued: '排队中',
  fetching: '读取链接',
  extracting: '解析链接',
  transcoding: '抽取音频',
  transcribing: '语音转写',
  ocr: '图片识别',
  packaging: '整理结果',
  done: '完成',
};

/* 解析器名 -> 展示名。后端返回 bilibili / ytdlp 这类短名。 */
const PLATFORM_LABEL = {
  bilibili: 'bilibili',
  youtube: 'YouTube',
  xiaoyuzhou: '小宇宙',
  ximalaya: '喜马拉雅',
  douyin: '抖音',
  xiaohongshu: '小红书',
  tiktok: 'TikTok',
  twitter: 'X',
  weibo: '微博',
  ytdlp: '通用 yt-dlp',
  generic: '通用解析',
};

const state = {
  caps: null,
  mode: 'single',
  file: null,
  taskId: null,
  task: null,
  es: null,
  pollTimer: null,
  tickTimer: null,
  startedAt: 0,
  tab: 'text',
  query: '',
  curMatch: -1,
  segAll: [],
  filtered: [],
  segRendered: 0,
  sel: new Set(),
  segDirty: true,
  rewriteText: '',
  batchId: null,
  batchTimer: null,
  batchTasks: [],
  histStatus: '',
  histQuery: '',
  histLimit: 30,
  histTotal: 0,
  paletteIndex: 0,
  paletteItems: [],
};

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

function fmtDur(sec) {
  if (!sec || !isFinite(sec)) return '—';
  const t = Math.round(sec);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function fmtClock(sec) {
  const t = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function fmtElapsed(ms) {
  const t = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(t / 60);
  const s = t % 60;
  const h = Math.floor(m / 60);
  const pad = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m % 60)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function fmtWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  if (sameDay) return `今天 ${hm}`;
  const yest = new Date(now);
  yest.setDate(now.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) return `昨天 ${hm}`;
  const md = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  return d.getFullYear() === now.getFullYear() ? `${md} ${hm}` : `${d.getFullYear()}-${md}`;
}

let toastTimer = null;
function toast(msg, kind) {
  const el = $('#toast');
  if (!el) return;
  el.textContent = String(msg ?? '');
  el.className = 'toast show' + (kind === 'err' ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, kind === 'err' ? 4200 : 2400);
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch { /* 非 JSON 错误体，保留状态码 */ }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

async function copy(text) {
  const value = String(text ?? '');
  if (!value) return false;
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    // 非 https / 无权限时退回 execCommand
    try {
      const ta = document.createElement('textarea');
      ta.value = value;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none;';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

function autosize(ta) {
  if (!ta) return;
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 240) + 'px';
}

/* 从一段文本里抠链接：支持直接粘 URL，也支持「【标题】 https://…」这种分享文案。 */
function extractUrls(text) {
  const raw = String(text || '');
  const found = raw.match(/https?:\/\/[^\s<>"'，。；、）】》]+/g) || [];
  const cleaned = found.map((u) => u.replace(/[.,;:!?)\]}>'"]+$/, ''));
  // 去掉纯裸域名的兜底：用户只贴 www.bilibili.com/xxx 时补协议
  if (!cleaned.length) {
    const bare = raw.match(/(?:^|\s)((?:[\w-]+\.)+(?:com|cn|net|org|tv|io|me|app|link|be)\/[^\s<>"']*)/i);
    if (bare) cleaned.push('https://' + bare[1]);
  }
  return [...new Set(cleaned)];
}

/* --------------------------------------------------------------- 2. 主题 */

function syncThemeBtn() {
  const dark = document.documentElement.dataset.theme === 'dark';
  const btn = $('#themeBtn');
  if (!btn) return;
  btn.innerHTML = `<svg class="ico" aria-hidden="true"><use href="#i-${dark ? 'sun' : 'moon'}"></use></svg>`;
  const label = dark ? '切换到浅色' : '切换到深色';
  btn.title = label;
  btn.setAttribute('aria-label', label);
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('c2t.theme', theme); } catch { /* 隐私模式忽略 */ }
  syncThemeBtn();
}

function toggleTheme() {
  setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
}

/* ------------------------------------------------------- 3. 能力探测与提示 */

function pill(text, kind, title) {
  const t = title ? ` title="${esc(title)}"` : '';
  return `<span class="pill ${kind || ''}"${t}><span class="dot"></span>${esc(text)}</span>`;
}

function prettyPlatform(name) {
  const key = String(name || '').toLowerCase();
  if (PLATFORM_LABEL[key]) return PLATFORM_LABEL[key];
  return key ? key.charAt(0).toUpperCase() + key.slice(1) : '';
}

/* 都接受可选的 caps 参数：渲染时传本次拿到的对象，避免读到一个更新的 state.caps
   导致「徽章说关、提示说开」这种自相矛盾。调用方不传就用当前快照。 */
function asrAvailable(caps) {
  const c = caps || state.caps;
  return !!(c && (c.asr || []).some((e) => e.available));
}

function extractorsEnabled(caps) {
  const c = caps || state.caps;
  const ex = c && c.extractors;
  /* 没拿到 capabilities 时不要擅自禁用：让后端去判断，前端不做假阳性拦截。 */
  if (!c || !ex) return true;
  return ex.enabled !== false;
}

function renderCaps(caps) {
  const box = $('#caps');
  const pills = [];
  const ff = caps.ffmpeg || {};

  pills.push(ff.available
    ? pill('ffmpeg', 'ok', `来源：${ff.source || '未知'}`)
    : pill('ffmpeg 缺失', 'bad', ff.reason || '未找到 ffmpeg'));

  const asr = (caps.asr || []).find((e) => e.available);
  if (asr) {
    pills.push(pill(asr.name, 'ok', asr.timestamps ? '支持时间戳' : '该引擎不返回时间戳'));
  } else {
    pills.push(pill('无 ASR', 'bad', '安装 faster-whisper 或 mlx-whisper'));
  }

  pills.push(caps.extractors && caps.extractors.enabled
    ? pill('链接解析', 'ok', '已开启')
    : pill('链接解析关', 'warn', 'EXTRACTORS_ENABLED=false'));

  if (caps.rewrite && caps.rewrite.available) pills.push(pill('AI 改写', 'ok'));

  box.innerHTML = pills.join('');
}

function renderPlatforms(caps) {
  const row = $('#platformRow');
  const ex = (caps && caps.extractors) || {};
  const limits = (caps && caps.limits) || {};

  if (!ex.enabled) {
    row.innerHTML = '<span class="pl-label">链接解析未开启</span>'
      + '<span class="plat off"><span class="dot"></span>仅支持上传本地文件</span>';
    return;
  }

  const list = (ex.platforms || []).filter((p) => p && p.available !== false);
  if (!list.length) {
    row.innerHTML = '<span class="pl-label">支持</span>'
      + '<span class="plat"><span class="dot"></span>常见音视频页面（yt-dlp）</span>';
    return;
  }

  /* 兜底的通用解析器放到最后，且文案弱化 —— 它不是「平台」。 */
  const named = list.filter((p) => !p.fallback);
  const generic = list.filter((p) => p.fallback);

  const chips = [];
  for (const p of named.slice(0, 8)) {
    /* 后端给了 label 就用 label（可能是「B 站」这种），否则退回名字映射 */
    const label = p.label || prettyPlatform(p.name);
    const hint = [p.kind, (p.hosts || []).join('\n')].filter(Boolean).join(' · ');
    chips.push(`<span class="plat" title="${esc(hint)}">`
      + `<span class="dot"></span>${esc(label)}</span>`);
  }
  for (const p of generic) {
    const label = p.name === 'ytdlp' ? '通用 yt-dlp' : (p.label || prettyPlatform(p.name));
    chips.push(`<span class="plat" title="没有专用解析器的站点走这里">`
      + `<span class="dot"></span>${esc(label)}</span>`);
  }
  if (limits.max_upload_mb) {
    chips.push(`<span class="plat off"><span class="dot"></span>本地文件 ≤ ${esc(limits.max_upload_mb)} MB</span>`);
  }

  row.innerHTML = '<span class="pl-label">支持</span>' + chips.join('');
}

function noticeHtml(kind, icon, html) {
  return `<div class="notice ${kind}"><svg class="ico" aria-hidden="true"><use href="#i-${icon}"></use></svg>`
    + `<div>${html}</div></div>`;
}

function renderNotices(caps) {
  const box = $('#extractNotice');
  const items = [];

  if (!asrAvailable(caps)) {
    const reason = (caps.asr || []).map((e) => `${e.name}: ${e.reason || '不可用'}`).join('；');
    items.push(noticeHtml('err', 'alert',
      `<b>没有可用的语音识别引擎。</b>安装 faster-whisper 或 mlx-whisper 后重启服务。`
      + (reason ? `<br><span class="hint">${esc(reason)}</span>` : '')));
  }

  if (!extractorsEnabled(caps)) {
    items.push(noticeHtml('', 'alert',
      `<b>链接解析当前关闭。</b>在 <code>.env</code> 里设置 <code>EXTRACTORS_ENABLED=true</code> `
      + `后重启服务即可解析链接；在此之前仍可以上传本地音视频。`));
  }

  if (!ffReady(caps)) {
    items.push(noticeHtml('', 'alert',
      `<b>没找到 ffmpeg。</b>转码和切分依赖它，先装 ffmpeg 或设置 <code>FFMPEG_MODE</code>。`));
  }

  box.innerHTML = items.join('');
  box.hidden = items.length === 0;
}

function ffReady(caps) {
  return !!(caps && caps.ffmpeg && caps.ffmpeg.available);
}

async function loadCapabilities() {
  try {
    const caps = await api('/capabilities');
    state.caps = caps;

    renderCaps(caps);
    renderPlatforms(caps);
    renderNotices(caps);

    const limits = caps.limits || {};
    if (limits.max_upload_mb) {
      const hint = $('#composerHint');
      if (hint && !state.file) hint.textContent = `⌘ / Ctrl + Enter 开始 · 单文件 ≤ ${limits.max_upload_mb} MB`;
    }
    if (limits.max_batch_size) {
      $('#batchHint').textContent = `每行一条链接，最多 ${limits.max_batch_size} 条，重复的自动去掉`;
    }
    autosize($('#batchInput'));

    // 有 bilibili 解析器时，占位符直接给个真实例子，降低第一次使用的门槛
    const hasBili = (caps.extractors && caps.extractors.enabled)
      && (caps.extractors.platforms || []).some((p) => p.name === 'bilibili' && p.available !== false);
    if (hasBili) $('#urlInput').placeholder = 'https://www.bilibili.com/video/BV…';

    if (!asrAvailable()) {
      $('#startBtn').disabled = true;
      $('#batchStart').disabled = true;
    }

    // 改写入口：没配 LLM 就整个隐藏，不做点了才报错的按钮
    if (caps.rewrite && caps.rewrite.available) {
      $('#rewriteTab').hidden = false;
      await loadStyles();
    }
  } catch (err) {
    $('#caps').innerHTML = pill('能力探测失败', 'bad', err.message);
    toast('无法读取本机能力：' + err.message, 'err');
  }
}

/* ------------------------------------------------------- 4. 输入与提交 */

function setMode(mode) {
  state.mode = mode;
  $$('.mode').forEach((m) => {
    const on = m.dataset.mode === mode;
    m.classList.toggle('on', on);
    m.setAttribute('aria-selected', String(on));
  });
  const single = mode === 'single';
  $('#composer').hidden = !single;
  $('#batchBox').hidden = single;
  if (single) {
    autosize($('#urlInput'));
    $('#urlInput').focus();
  } else {
    autosize($('#batchInput'));
    $('#batchInput').focus();
  }
}

function setFile(file) {
  if (!file) return;
  state.file = file;
  $('#fileName').textContent = file.name;
  $('#fileSize').textContent = humanSize(file.size);
  $('#fileChip').hidden = false;
  $('#uploadBar').style.width = '0%';
  $('#uploadPct').textContent = '';
  autosize($('#urlInput'));
  $('#composerHint').textContent = '文件已就绪，点「开始提取」上传并转写';
}

function clearFile() {
  state.file = null;
  const input = $('#fileInput');
  if (input) input.value = '';
  $('#fileChip').hidden = true;
  $('#uploadBar').style.width = '0%';
  $('#uploadPct').textContent = '';
  const mb = state.caps && state.caps.limits && state.caps.limits.max_upload_mb;
  $('#composerHint').textContent = mb
    ? `⌘ / Ctrl + Enter 开始 · 单文件 ≤ ${mb} MB`
    : '⌘ / Ctrl + Enter 开始';
}

function humanSize(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 ** 2) return (n / 1024).toFixed(0) + ' KB';
  if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1) + ' MB';
  return (n / 1024 ** 3).toFixed(2) + ' GB';
}

function resetComposerBusy(busy) {
  $('#startBtn').disabled = busy || !asrAvailable();
  $('#batchStart').disabled = busy || !asrAvailable();
  $('#composer').classList.toggle('is-busy', busy);
  $('#startBtn').innerHTML = busy
    ? '提交中…'
    : '开始提取<svg class="ico" aria-hidden="true"><use href="#i-arrow"></use></svg>';
}

async function submitSingle() {
  if (!asrAvailable()) {
    toast('没有可用的语音识别引擎', 'err');
    return;
  }

  if (state.file) return startUploadTask();

  const raw = $('#urlInput').value.trim();
  if (!raw) {
    $('#urlInput').focus();
    toast('先粘贴一条链接');
    return;
  }

  const urls = extractUrls(raw);
  if (!urls.length) {
    toast('没识别到链接，请粘贴 http(s) 开头的地址', 'err');
    return;
  }

  // 粘了一堆链接却停在单条模式：自动切到批量，别让用户自己发现
  if (urls.length > 1) {
    setMode('batch');
    $('#batchInput').value = urls.join('\n');
    autosize($('#batchInput'));
    toast(`识别到 ${urls.length} 条链接，已切到批量模式`);
    return;
  }

  if (!extractorsEnabled()) {
    renderNotices(state.caps || {});
    $('#extractNotice').scrollIntoView({ behavior: 'smooth', block: 'center' });
    toast('链接解析未开启', 'err');
    return;
  }

  resetComposerBusy(true);
  try {
    const task = await api('/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_type: 'url', source_value: urls[0] }),
    });
    $('#urlInput').value = '';
    autosize($('#urlInput'));
    watchTask(task.id, task);
  } catch (err) {
    toast(err.message, 'err');
    resetComposerBusy(false);
  }
}

async function startUploadTask() {
  const file = state.file;
  if (!file) return;
  resetComposerBusy(true);
  $('#uploadBar').style.width = '0%';
  $('#uploadPct').textContent = '上传 0%';

  try {
    const up = await upload(file, (pct) => {
      $('#uploadBar').style.width = pct + '%';
      $('#uploadPct').textContent = `上传 ${pct}%`;
    });
    $('#uploadPct').textContent = '已上传';
    const task = await api('/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_type: 'upload',
        source_value: up.path,
        title: file.name.replace(/\.[^.]+$/, ''),
        work_type: up.work_type,
      }),
    });
    clearFile();
    watchTask(task.id, task);
  } catch (err) {
    toast(err.message, 'err');
    $('#uploadPct').textContent = '失败';
    resetComposerBusy(false);
  }
}

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
        let detail = `上传失败（${xhr.status}）`;
        try { detail = JSON.parse(xhr.responseText).detail || detail; } catch { /* 忽略 */ }
        reject(new Error(detail));
      }
    };
    xhr.onerror = () => reject(new Error('网络错误，上传中断'));
    xhr.ontimeout = () => reject(new Error('上传超时'));
    const fd = new FormData();
    fd.append('file', file, file.name);
    xhr.send(fd);
  });
}

/* --------------------------------------------------------- 5. 任务进度 */

function estimateChunks(task) {
  const max = state.caps && state.caps.limits && state.caps.limits.chunk_max_sec;
  const dur = Number(task.duration) || 0;
  if (!max || !dur) return 0;
  return Math.max(1, Math.ceil(dur / max));
}

function stageInfo(task) {
  const stage = task.stage || 'queued';
  const p = Number(task.progress) || 0;

  if (task.status === 'QUEUED') {
    return { label: '排队中', detail: '等待空闲的转写线程…' };
  }
  if (task.status === 'FAILED') {
    return { label: '失败', detail: task.error_message || '任务失败，没有给出原因。' };
  }
  if (task.status === 'CANCELED') {
    return { label: '已取消', detail: '任务已取消。' };
  }

  switch (stage) {
    case 'fetching':
      return { label: '读取链接', detail: '在打开链接、读取响应头…' };
    case 'extracting':
      return { label: '解析链接', detail: '解析视频流地址与元信息，长视频这一步要等一会儿。' };
    case 'transcoding':
      return { label: '抽取音频', detail: '用 ffmpeg 抽出音轨并按片切分…' };
    case 'transcribing': {
      const total = estimateChunks(task);
      if (total) {
        const done = Math.min(total, Math.max(1, Math.round(((p - 30) / 60) * total)));
        return {
          label: '语音转写',
          detail: `第 ${done} / ${total} 段音频（按进度估算）· 音频时长 ${fmtDur(task.duration)}`,
        };
      }
      return { label: '语音转写', detail: '逐段识别中。两小时的播客通常要 10 分钟以上，可以先去忙别的。' };
    }
    case 'ocr':
      return { label: '图片识别', detail: '正在识别图片中的文字…' };
    case 'packaging':
      return { label: '整理结果', detail: '合并分句、准备导出数据…' };
    case 'done':
      return { label: '完成', detail: '' };
    default:
      return { label: STAGE_LABEL[stage] || stage, detail: '' };
  }
}

function renderStepper(stepIndex, finished) {
  const box = $('#stepper');
  box.innerHTML = STEPS.map((s, i) => {
    let cls = 'step';
    if (finished || i < stepIndex) cls += ' done';
    else if (i === stepIndex) cls += ' active';
    return `<li class="${cls}"><span class="step-dot" aria-hidden="true"></span>`
      + `<span class="step-label">${esc(s.label)}</span></li>`;
  }).join('');
}

function showTaskPanel() {
  $('#taskPanel').hidden = false;
  $('#resultPanel').hidden = true;
}

function renderTaskPanel(task) {
  const info = stageInfo(task);
  const pct = Math.max(0, Math.min(100, Number(task.progress) || 0));
  const finished = task.status === 'SUCCESS';
  const failed = task.status === 'FAILED' || task.status === 'CANCELED';

  $('#taskStageLabel').textContent = info.label;
  $('#taskPct').textContent = failed ? '—' : pct + '%';
  $('#taskBar').style.width = (finished ? 100 : pct) + '%';

  const progress = $('#taskProgress');
  progress.setAttribute('aria-valuenow', String(pct));

  const dot = $('#taskDot');
  dot.className = 'live-dot'
    + (finished ? ' done' : failed ? ' fail' : task.status === 'RUNNING' ? '' : ' idle');

  const detail = $('#taskDetail');
  detail.textContent = info.detail || '';
  detail.classList.toggle('err', failed);

  $('#cancelBtn').hidden = ['SUCCESS', 'FAILED', 'CANCELED'].includes(task.status);

  renderStepper(STAGE_STEP[task.stage] ?? 0, finished);
}

function startTicker() {
  stopTicker();
  state.tickTimer = setInterval(() => {
    const el = $('#taskElapsed');
    if (!el || !state.startedAt) return;
    el.textContent = fmtElapsed(Date.now() - state.startedAt);
  }, 1000);
}

function stopTicker() {
  if (state.tickTimer) {
    clearInterval(state.tickTimer);
    state.tickTimer = null;
  }
}

function closeWatch() {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  stopTicker();
}

/*
 * 进度用 SSE（后端 /tasks/{id}/events 会推 task / done 两种事件）。
 * EventSource 断线会自己重连，连续失败两次才退回轮询 —— 本地服务基本用不到。
 */
function watchTask(taskId, initialTask) {
  closeWatch();
  state.taskId = taskId;
  state.task = initialTask || null;

  const created = initialTask && initialTask.created_at ? new Date(initialTask.created_at).getTime() : NaN;
  state.startedAt = isNaN(created) ? Date.now() : Math.min(Date.now(), created);

  showTaskPanel();
  if (initialTask) renderTaskPanel(initialTask);
  else {
    renderTaskPanel({ status: 'QUEUED', stage: 'queued', progress: 0 });
  }
  $('#taskElapsed').textContent = '00:00';
  startTicker();
  resetComposerBusy(true);

  let errors = 0;
  try {
    const es = new EventSource(`${API}/tasks/${taskId}/events`);
    state.es = es;
    es.addEventListener('task', (ev) => {
      let task;
      try { task = JSON.parse(ev.data); } catch { return; }
      onTask(task);
    });
    es.addEventListener('done', () => {
      es.close();
      if (state.es === es) state.es = null;
    });
    es.onerror = () => {
      if (state.es !== es) return; // 已经关掉了，别重复降级
      errors += 1;
      if (errors >= 2) {
        es.close();
        state.es = null;
        pollTask(taskId);
      }
    };
  } catch {
    pollTask(taskId);
  }
}

function pollTask(taskId) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  const tick = async () => {
    try {
      onTask(await api(`/tasks/${taskId}`));
    } catch (err) {
      closeWatch();
      toast(err.message, 'err');
      resetComposerBusy(false);
    }
  };
  tick();
  state.pollTimer = setInterval(tick, 2500);
}

function onTask(task) {
  state.task = task;
  renderTaskPanel(task);

  if (task.status === 'SUCCESS') {
    closeWatch();
    resetComposerBusy(false);
    renderResult(task);
    loadHistory();
    toast('转写完成');
  } else if (task.status === 'FAILED' || task.status === 'CANCELED') {
    closeWatch();
    resetComposerBusy(false);
    if (task.status === 'FAILED') toast(task.error_message || '任务失败', 'err');
    loadHistory();
  }
}

async function cancelTask() {
  if (!state.taskId) return;
  try {
    await api(`/tasks/${state.taskId}/cancel`, { method: 'POST' });
    $('#taskDetail').textContent = '取消中…';
  } catch (err) {
    toast(err.message, 'err');
  }
}

/* --------------------------------------------------------- 6. 结果渲染 */

function paragraphize(text, target) {
  const size = target || 300;
  const out = [];
  for (const block of String(text).split(/\n+/)) {
    const b = block.trim();
    if (!b) continue;
    if (b.length <= size * 1.2) { out.push(b); continue; }
    const pieces = b.match(/[^。！？!?；;：:]+[。！？!?；;：:]?/g) || [b];
    let buf = '';
    for (const piece of pieces) {
      if (buf.length + piece.length > size && buf) { out.push(buf); buf = ''; }
      buf += piece;
    }
    if (buf.trim()) out.push(buf);
  }
  return out;
}

function highlight(text, q) {
  const s = String(text ?? '');
  if (!q) return { html: esc(s), count: 0 };
  const lower = s.toLowerCase();
  const needle = q.toLowerCase();
  let out = '';
  let i = 0;
  let count = 0;
  while (count < 800) {
    const j = lower.indexOf(needle, i);
    if (j < 0) break;
    out += esc(s.slice(i, j)) + '<mark>' + esc(s.slice(j, j + needle.length)) + '</mark>';
    i = j + needle.length;
    count += 1;
  }
  out += esc(s.slice(i));
  return { html: out, count };
}

function setSearchCount(text, active) {
  const el = $('#searchCount');
  el.textContent = text || '';
  $('#searchPrev').disabled = !active;
  $('#searchNext').disabled = !active;
}

function renderDoc() {
  const task = state.task;
  const box = $('#docText');
  const text = (task && (task.text_content || task.content)) || '';

  if (!text) {
    box.innerHTML = '<p class="doc-empty">这个任务没有返回文字内容。</p>';
    setSearchCount('', false);
    return;
  }

  let count = 0;
  box.innerHTML = paragraphize(text).map((p) => {
    const r = highlight(p, state.query);
    count += r.count;
    return `<p>${r.html}</p>`;
  }).join('');

  state.curMatch = -1;
  if (state.query) {
    setSearchCount(count ? `${count} 处` : '无匹配', count > 0);
  } else {
    setSearchCount('', false);
  }
}

function renderSegments() {
  const all = state.segAll;
  const q = state.query;
  state.filtered = q
    ? all.filter((s) => String(s.text || '').toLowerCase().includes(q.toLowerCase()))
    : all.slice();

  const list = $('#segList');
  list.innerHTML = '';
  state.segRendered = 0;
  state.sel.clear();
  updateSegStatus();

  if (!all.length) {
    list.innerHTML = '<div class="seg-empty">当前引擎不返回带时间戳的分句。切到「正文」看全文，或换一个支持时间戳的 ASR 引擎。</div>';
    $('#segSentinel').hidden = true;
    return;
  }
  if (!state.filtered.length) {
    list.innerHTML = `<div class="seg-empty">没有包含「${esc(q)}」的分句</div>`;
    $('#segSentinel').hidden = true;
    return;
  }

  appendSegChunk();
}

function appendSegChunk() {
  const list = $('#segList');
  const total = state.filtered.length;
  if (state.segRendered >= total) return;

  const size = state.query ? SEG_CHUNK_SEARCH : SEG_CHUNK;
  const end = Math.min(total, state.segRendered + size);
  const rows = [];

  for (let k = state.segRendered; k < end; k += 1) {
    const s = state.filtered[k];
    const hit = highlight(s.text, state.query);
    rows.push(
      `<div class="seg" data-k="${k}">`
      + `<button class="seg-time" type="button" data-time="${k}" title="复制这一句">${fmtClock(s.start)}</button>`
      + `<span class="seg-text">${hit.html}</span>`
      + '</div>',
    );
  }

  list.insertAdjacentHTML('beforeend', rows.join(''));
  state.segRendered = end;
  $('#segSentinel').hidden = end >= total;
  updateSegStatus();
}

function updateSegStatus() {
  const total = state.segAll.length;
  const shown = state.filtered.length;
  const el = $('#segStatus');
  if (!total) { el.textContent = '无分句'; return; }
  const parts = [];
  parts.push(state.query ? `命中 ${shown} / ${total} 句` : `共 ${total} 句`);
  if (state.segRendered < shown) parts.push(`已渲染 ${state.segRendered}`);
  el.textContent = parts.join(' · ');

  const btn = $('#copySelectedSegs');
  btn.disabled = state.sel.size === 0;
  btn.textContent = state.sel.size ? `复制选中 (${state.sel.size})` : '复制选中';
}

function setTab(name) {
  state.tab = name;
  $$('.tab').forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle('on', on);
    t.setAttribute('aria-selected', String(on));
  });
  $('#panelText').hidden = name !== 'text';
  $('#panelSegments').hidden = name !== 'segments';
  $('#panelRewrite').hidden = name !== 'rewrite';

  if (name === 'segments') {
    if (state.segDirty) {
      state.segDirty = false;
      renderSegments();
    } else {
      updateSegStatus();
    }
  }
  if (name === 'text') renderDoc();
  if (name === 'rewrite') {
    $('#searchBox').style.visibility = 'hidden';
  } else {
    $('#searchBox').style.visibility = '';
  }
}

function renderResult(task) {
  state.task = task;
  state.taskId = task.id;

  $('#resultPanel').hidden = false;
  $('#taskPanel').hidden = true;

  const title = (task.title || '').trim()
    || (task.source_value || '').replace(/^.*\//, '').slice(0, 80)
    || '未命名结果';
  $('#resultTitle').textContent = title;
  $('#resultTitle').title = title;

  const meta = [];
  if (task.duration) meta.push(`<span class="meta-item"><b>${fmtDur(task.duration)}</b>时长</span>`);
  if (task.text_content) {
    meta.push(`<span class="meta-item"><b>${task.text_content.length.toLocaleString('zh-CN')}</b>字</span>`);
  }
  if (task.segments && task.segments.length) {
    meta.push(`<span class="meta-item"><b>${task.segments.length}</b>分句</span>`);
  }
  if (task.language) meta.push(`<span class="meta-item"><b>${esc(task.language)}</b>语言</span>`);
  if (task.engine) meta.push(`<span class="meta-item"><b>${esc(task.engine)}</b>引擎</span>`);
  if (task.platform) meta.push(`<span class="meta-item"><b>${esc(prettyPlatform(task.platform))}</b>来源</span>`);
  const when = fmtWhen(task.created_at);
  if (when) meta.push(`<span class="meta-time">${esc(when)}</span>`);
  $('#resultMeta').innerHTML = meta.join('');

  state.segAll = Array.isArray(task.segments) ? task.segments : [];
  state.segDirty = true;
  state.query = '';
  $('#searchInput').value = '';
  setSearchCount('', false);
  $('#segCount').textContent = state.segAll.length ? ` ${state.segAll.length}` : '';
  $('#segCopyHint').textContent = '';

  // 换任务时清掉上一轮的改写，避免张冠李戴
  state.rewriteText = '';
  $('#rewriteOutput').textContent = '';
  $('#rewriteStatus').textContent = '';
  $('#rewriteCount').textContent = '';

  setTab('text');
  $('#resultPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ------------------------------------------------------------ 7. 搜索 */

function onQueryChange() {
  state.query = $('#searchInput').value.trim();
  state.curMatch = -1;
  if (state.tab === 'segments') {
    renderSegments();
    const n = state.filtered.length;
    setSearchCount(state.query ? (n ? `命中 ${n} 句` : '无匹配') : '', n > 0 && !!state.query);
  } else if (state.tab === 'text') {
    renderDoc();
  }
}

function jumpMatch(dir) {
  const scope = state.tab === 'segments' ? $('#panelSegments') : $('#panelText');
  const marks = $$('mark', scope);
  if (!marks.length) {
    toast('没有匹配的内容');
    return;
  }
  state.curMatch = (state.curMatch + dir + marks.length) % marks.length;
  marks.forEach((m) => m.classList.remove('cur'));
  const m = marks[state.curMatch];
  m.classList.add('cur');
  m.scrollIntoView({ block: 'center', behavior: 'smooth' });
  setSearchCount(`${state.curMatch + 1} / ${marks.length}`, true);
}

/* ------------------------------------------------------------ 8. 导出 */

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function filenameFrom(res, fallback) {
  const cd = res.headers.get('Content-Disposition') || '';
  const m = cd.match(/filename\*=UTF-8''([^;]+)/i);
  if (m) {
    try { return decodeURIComponent(m[1]); } catch { /* 用默认名 */ }
  }
  const plain = cd.match(/filename="?([^";]+)"?/i);
  if (plain) return plain[1];
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
      let detail = `导出失败（${res.status}）`;
      try { detail = (await res.json()).detail || detail; } catch { /* 忽略 */ }
      throw new Error(detail);
    }
    const blob = await res.blob();
    const ext = format === 'markdown' ? 'md' : format;
    const fallback = `clip2text-${String(state.taskId).slice(0, 8)}.${ext}`;
    const name = filenameFrom(res, fallback);
    triggerDownload(blob, name);
    toast(`已导出 ${name}`);
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function copyFullText() {
  const text = (state.task && (state.task.text_content || state.task.content)) || '';
  if (!text) { toast('没有可复制的内容'); return; }
  const ok = await copy(text);
  toast(ok ? `已复制 ${text.length.toLocaleString('zh-CN')} 字` : '复制失败，请手动选择', ok ? '' : 'err');
}

async function copySegments(onlySelected) {
  const list = onlySelected
    ? Array.from(state.sel).sort((a, b) => a - b).map((k) => state.filtered[k])
    : state.segAll;
  if (!list.length) { toast('没有可复制的分句'); return; }
  const text = list.map((s) => `[${fmtClock(s.start)}] ${s.text}`).join('\n');
  const ok = await copy(text);
  $('#segCopyHint').textContent = ok ? `已复制 ${list.length} 句` : '复制失败';
  setTimeout(() => { $('#segCopyHint').textContent = ''; }, 2200);
  if (ok) toast(`已复制 ${list.length} 句`);
}

/* ------------------------------------------------------------ 9. 改写 */

async function loadStyles() {
  try {
    const data = await api('/rewrite/styles');
    const sel = $('#styleSelect');
    sel.innerHTML = (data.styles || [])
      .map((s) => `<option value="${esc(s.key)}" title="${esc(s.description || '')}">${esc(s.label)}</option>`)
      .join('');
    if (data.default) sel.value = data.default;
  } catch (err) {
    console.warn('加载改写风格失败', err);
  }
}

async function doRewrite() {
  if (!state.taskId) { toast('没有可改写的任务'); return; }
  const style = $('#styleSelect').value;
  const instruction = $('#rewriteInstruction').value.trim();
  const out = $('#rewriteOutput');
  const status = $('#rewriteStatus');
  const btn = $('#doRewrite');

  state.rewriteText = '';
  out.textContent = '';
  $('#rewriteCount').textContent = '';
  status.textContent = '连接模型…';
  status.className = 'hint';
  btn.disabled = true;

  try {
    const res = await fetch(`${API}/rewrite`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        task_id: state.taskId,
        style,
        ...(instruction ? { instruction } : {}),
      }),
    });
    if (!res.ok) {
      let detail = `请求失败（${res.status}）`;
      try { detail = (await res.json()).detail || detail; } catch { /* 忽略 */ }
      throw new Error(detail);
    }

    // POST + SSE 不能用 EventSource，手写一个最小解析器
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let finished = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const blocks = buf.split('\n\n');
      buf = blocks.pop() || '';

      for (const block of blocks) {
        const evMatch = block.match(/^event:\s*(\w+)/m);
        const dataMatch = block.match(/^data:\s*(.*)$/m);
        if (!dataMatch) continue;
        let payload;
        try { payload = JSON.parse(dataMatch[1]); } catch { continue; }
        const ev = evMatch ? evMatch[1] : 'message';

        if (ev === 'delta' && payload.text) {
          state.rewriteText += payload.text;
          out.textContent = state.rewriteText;
          out.scrollTop = out.scrollHeight;
          $('#rewriteCount').textContent = `${state.rewriteText.length.toLocaleString('zh-CN')} 字`;
        } else if (ev === 'error') {
          throw new Error(payload.detail || '改写失败');
        } else if (ev === 'done') {
          finished = true;
        }
      }
    }

    status.textContent = finished ? '完成' : '结束';
    status.className = 'hint';
    if (!state.rewriteText) status.textContent = '模型没有返回内容';
  } catch (err) {
    status.textContent = err.message;
    status.style.color = 'var(--err)';
    toast(err.message, 'err');
  } finally {
    btn.disabled = false;
    setTimeout(() => { status.style.color = ''; }, 200);
  }
}

/* ------------------------------------------------------------ 10. 批量 */

async function startBatch() {
  const text = $('#batchInput').value.trim();
  if (!text) { toast('先粘贴几条链接'); $('#batchInput').focus(); return; }

  const urls = extractUrls(text);
  if (!urls.length) { toast('没识别到链接', 'err'); return; }

  if (!extractorsEnabled()) {
    renderNotices(state.caps || {});
    $('#extractNotice').scrollIntoView({ behavior: 'smooth', block: 'center' });
    toast('链接解析未开启', 'err');
    return;
  }

  resetComposerBusy(true);
  $('#batchPanel').hidden = false;
  $('#batchBar').style.width = '0%';
  $('#batchSummary').textContent = '提交中…';
  $('#batchList').innerHTML = '';
  $('#batchPanel').scrollIntoView({ behavior: 'smooth', block: 'center' });

  try {
    const batch = await api('/batches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_type: 'url', text: urls.join('\n') }),
    });

    state.batchId = batch.id;
    state.batchTasks = [];

    const notes = [];
    if (batch.duplicates && batch.duplicates.length) notes.push(`${batch.duplicates.length} 条重复已跳过`);
    if (batch.invalid && batch.invalid.length) notes.push(`${batch.invalid.length} 条无效已跳过`);
    $('#batchSummary').textContent = `已提交 ${batch.total} 条${notes.length ? '（' + notes.join('，') + '）' : ''}`;

    pollBatch(batch.id);
  } catch (err) {
    toast(err.message, 'err');
    $('#batchSummary').textContent = '提交失败';
    resetComposerBusy(false);
  }
}

function pollBatch(batchId) {
  stopBatchPolling();
  const tick = async () => {
    try {
      const data = await api(`/batches/${batchId}`);
      const b = data.batch || {};
      state.batchTasks = data.tasks || [];

      const pct = Math.max(0, Math.min(100, Number(data.progress) || 0));
      $('#batchBar').style.width = pct + '%';
      $('#batchProgress').setAttribute('aria-valuenow', String(pct));
      $('#batchSummary').textContent =
        `${b.succeeded || 0} 成功 / ${b.failed || 0} 失败 / 共 ${b.total || 0} · ${pct}%`;

      renderBatchList();

      if (['DONE', 'PARTIAL', 'FAILED', 'CANCELED'].includes(b.status)) {
        stopBatchPolling();
        resetComposerBusy(false);
        $('#batchCancel').hidden = true;
        const label = STATUS_LABEL[b.status] || b.status;
        $('#batchSummary').textContent = `${label} · ${b.succeeded || 0} / ${b.total || 0}`;
        loadHistory();
      }
    } catch (err) {
      stopBatchPolling();
      toast(err.message, 'err');
      resetComposerBusy(false);
    }
  };
  tick();
  state.batchTimer = setInterval(tick, 2000);
}

function stopBatchPolling() {
  if (state.batchTimer) {
    clearInterval(state.batchTimer);
    state.batchTimer = null;
  }
}

function renderBatchList() {
  const box = $('#batchList');
  const tasks = state.batchTasks;
  if (!tasks.length) {
    box.innerHTML = '<div class="list-empty">还没有条目</div>';
    return;
  }

  box.innerHTML = tasks.map((t) => {
    const name = t.title || t.source_value || t.id;
    let sub;
    if (t.status === 'SUCCESS') {
      sub = `${(t.text_content || '').length} 字${t.duration ? ' · ' + fmtDur(t.duration) : ''}`;
    } else if (t.status === 'FAILED') {
      sub = t.error_message || '失败';
    } else if (t.status === 'CANCELED') {
      sub = '已取消';
    } else {
      sub = `${STAGE_LABEL[t.stage] || t.stage || '排队'} · ${t.progress || 0}%`;
    }
    const canOpen = t.status === 'SUCCESS';
    return `<div class="batch-item" data-task="${esc(t.id)}">
      <span class="status ${esc(t.status)}"><span class="dot"></span>${esc(STATUS_LABEL[t.status] || t.status)}</span>
      <span class="bi-main">
        <span class="bi-title">${esc(name)}</span>
        <span class="bi-sub">${esc(sub)}</span>
      </span>
      <span class="bi-actions">
        ${canOpen ? `<button class="btn btn-quiet btn-sm" type="button" data-open="${esc(t.id)}">打开</button>` : ''}
        ${canOpen ? `<button class="btn btn-quiet btn-sm" type="button" data-export="${esc(t.id)}" data-format="txt">TXT</button>` : ''}
      </span>
    </div>`;
  }).join('');
}

async function cancelBatch() {
  if (!state.batchId) return;
  try {
    await api(`/batches/${state.batchId}/cancel`, { method: 'POST' });
    $('#batchSummary').textContent = '取消中…';
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function batchExport(format) {
  const ok = state.batchTasks.filter((t) => t.status === 'SUCCESS');
  if (!ok.length) { toast('没有成功的条目可导出'); return; }

  const parts = [];
  if (format === 'txt') {
    for (const t of ok) {
      parts.push('='.repeat(52));
      parts.push(t.title || t.source_value);
      parts.push('='.repeat(52), '', t.text_content || '', '');
    }
  } else {
    parts.push(`# 批量导出（${ok.length} 条）`, '');
    for (const t of ok) {
      parts.push(`## ${t.title || t.source_value}`, '');
      if (t.duration) parts.push(`- 时长：${fmtDur(t.duration)}`);
      parts.push(`- 字数：${(t.text_content || '').length}`, '');
      parts.push(t.text_content || '', '');
    }
  }

  const blob = new Blob([parts.join('\n')], { type: 'text/plain;charset=utf-8' });
  triggerDownload(blob, `clip2text-batch.${format === 'markdown' ? 'md' : 'txt'}`);
  toast(`已导出 ${ok.length} 条`);
}

/* ------------------------------------------------------------ 11. 历史 */

function histItemHtml(t) {
  const name = t.title || (t.source_value || '').replace(/^.*\//, '') || t.id.slice(0, 8);
  const bits = [];
  if (t.platform) bits.push(esc(prettyPlatform(t.platform)));
  if (t.duration) bits.push(fmtDur(t.duration));
  if (t.text_content) bits.push(`${t.text_content.length.toLocaleString('zh-CN')} 字`);
  bits.push(esc(fmtWhen(t.created_at)));

  return `<div class="hist-item" data-id="${esc(t.id)}">
    <span class="status ${esc(t.status)}"><span class="dot"></span>${esc(STATUS_LABEL[t.status] || t.status)}</span>
    <button class="hist-main" type="button" data-open="${esc(t.id)}">
      <span class="hist-title">${esc(name)}</span>
      <span class="hist-sub">${bits.join('<span class="sep">·</span>')}</span>
    </button>
    <div class="hist-actions">
      <button class="iconbtn iconbtn-sm" type="button" data-del="${esc(t.id)}" title="删除这条记录" aria-label="删除">
        <svg class="ico" aria-hidden="true"><use href="#i-trash"></use></svg>
      </button>
    </div>
  </div>`;
}

async function loadHistory(reset) {
  if (reset !== false) {
    state.histLimit = 30;
  }
  const box = $('#history');
  const params = new URLSearchParams();
  params.set('limit', String(state.histLimit));
  if (state.histStatus) params.set('status', state.histStatus);
  if (state.histQuery) params.set('q', state.histQuery);

  try {
    const data = await api('/tasks?' + params.toString());
    state.histTotal = data.total || 0;
    const items = data.items || [];

    $('#histCount').textContent = data.total ? ` ${data.total}` : '';

    if (!items.length) {
      box.innerHTML = `<div class="hist-empty">${state.histQuery || state.histStatus
        ? '没有符合条件的记录' : '还没有记录。粘贴一个链接就能开始。'}</div>`;
      $('#histMore').hidden = true;
      return;
    }

    box.innerHTML = items.map(histItemHtml).join('');
    $('#histMore').hidden = items.length >= state.histTotal;
  } catch (err) {
    box.innerHTML = `<div class="hist-empty">加载失败：${esc(err.message)}</div>`;
    $('#histMore').hidden = true;
  }
}

async function openTask(id) {
  if (!id) return;
  closePalette();
  try {
    const task = await api(`/tasks/${id}`);
    if (task.status === 'SUCCESS') {
      renderResult(task);
    } else if (task.status === 'RUNNING' || task.status === 'QUEUED') {
      watchTask(task.id, task);
      toast('这个任务还在进行中');
    } else {
      toast(task.error_message || STATUS_LABEL[task.status] || '任务不可用', 'err');
    }
  } catch (err) {
    toast(err.message, 'err');
  }
}

function armDelete(btn, id) {
  if (btn.dataset.armed === '1') {
    btn.dataset.armed = '';
    deleteTask(id);
    return;
  }
  btn.dataset.armed = '1';
  btn.classList.add('btn-danger');
  btn.title = '再点一次删除';
  const original = btn.innerHTML;
  btn.innerHTML = '<span style="font-size:11px">确认</span>';
  setTimeout(() => {
    if (btn.isConnected) {
      btn.innerHTML = original;
      btn.title = '删除这条记录';
      btn.classList.remove('btn-danger');
      btn.dataset.armed = '';
    }
  }, 2800);
}

async function deleteTask(id) {
  try {
    await api(`/tasks/${id}`, { method: 'DELETE' });
    if (state.taskId === id) {
      state.taskId = null;
      state.task = null;
      $('#resultPanel').hidden = true;
      $('#taskPanel').hidden = true;
    }
    toast('已删除');
    loadHistory(false);
  } catch (err) {
    toast(err.message, 'err');
  }
}

/* -------------------------------------------------------- 12. 命令面板 */

const COMMANDS = [
  {
    id: 'paste',
    label: '从剪贴板粘贴并提交',
    icon: 'link',
    hint: '⌘V',
    run: async () => {
      setMode('single');
      $('#urlInput').focus();
      try {
        const text = await navigator.clipboard.readText();
        if (!text) { toast('剪贴板是空的'); return; }
        $('#urlInput').value = text;
        autosize($('#urlInput'));
        toast('已粘贴，确认后开始提取');
      } catch {
        toast('浏览器不允许读剪贴板，直接按 ⌘V 即可');
      }
    },
  },
  {
    id: 'upload',
    label: '上传本地文件',
    icon: 'upload',
    run: () => { setMode('single'); $('#fileInput').click(); },
  },
  {
    id: 'batch',
    label: '切换到批量链接',
    icon: 'list',
    run: () => setMode('batch'),
  },
  {
    id: 'search',
    label: '在结果中查找',
    icon: 'search',
    hint: '/',
    run: () => {
      if ($('#resultPanel').hidden) { toast('还没有结果'); return; }
      $('#searchInput').focus();
    },
  },
  {
    id: 'theme',
    label: '切换深浅色',
    icon: 'moon',
    run: () => toggleTheme(),
  },
  {
    id: 'history',
    label: '刷新历史记录',
    icon: 'refresh',
    run: () => loadHistory(),
  },
  {
    id: 'latest',
    label: '打开最近一次结果',
    icon: 'clock',
    run: async () => {
      try {
        const data = await api('/tasks?status=SUCCESS&limit=1');
        const t = (data.items || [])[0];
        if (t) openTask(t.id);
        else toast('还没有成功的结果');
      } catch (err) { toast(err.message, 'err'); }
    },
  },
  {
    id: 'top',
    label: '回到顶部粘贴框',
    icon: 'arrow',
    run: () => { window.scrollTo({ top: 0, behavior: 'smooth' }); setTimeout(() => $('#urlInput').focus(), 300); },
  },
];

function openPalette() {
  $('#paletteWrap').hidden = false;
  $('#paletteInput').value = '';
  state.paletteIndex = 0;
  renderPalette('');
  $('#paletteInput').focus();
}

function closePalette() {
  if ($('#paletteWrap').hidden) return;
  $('#paletteWrap').hidden = true;
}

function renderPalette(query) {
  const q = String(query || '').toLowerCase();
  state.paletteItems = COMMANDS.filter((c) => !q || c.label.toLowerCase().includes(q));
  state.paletteIndex = Math.min(state.paletteIndex, Math.max(0, state.paletteItems.length - 1));

  const box = $('#paletteList');
  if (!state.paletteItems.length) {
    box.innerHTML = '<div class="palette-empty">没有匹配的命令</div>';
    return;
  }
  box.innerHTML = state.paletteItems.map((c, i) => `
    <button class="palette-item${i === state.paletteIndex ? ' on' : ''}" type="button" data-i="${i}" role="option">
      <svg class="ico" aria-hidden="true"><use href="#i-${c.icon}"></use></svg>
      <span class="pi-label">${esc(c.label)}</span>
      ${c.hint ? `<span class="pi-hint">${esc(c.hint)}</span>` : ''}
    </button>`).join('');

  const active = box.querySelector('.palette-item.on');
  if (active) active.scrollIntoView({ block: 'nearest' });
}

function runCommand(cmd) {
  if (!cmd) return;
  closePalette();
  try {
    const r = cmd.run();
    if (r && typeof r.catch === 'function') r.catch((e) => toast(e.message, 'err'));
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* ------------------------------------------------------------ 事件绑定 */

function bind() {
  // ---- 主题 / 命令面板
  $('#themeBtn').addEventListener('click', toggleTheme);
  $('#paletteBtn').addEventListener('click', openPalette);
  $('#paletteWrap').addEventListener('click', (e) => {
    if (e.target === $('#paletteWrap')) closePalette();
  });
  $('#paletteInput').addEventListener('input', (e) => {
    state.paletteIndex = 0;
    renderPalette(e.target.value);
  });
  $('#paletteList').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-i]');
    if (btn) runCommand(state.paletteItems[Number(btn.dataset.i)]);
  });

  // ---- 模式
  $$('.mode').forEach((m) => {
    m.addEventListener('click', () => setMode(m.dataset.mode));
  });

  // ---- 粘贴框
  const urlInput = $('#urlInput');
  urlInput.addEventListener('input', () => autosize(urlInput));
  urlInput.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      submitSingle();
    }
  });

  const batchInput = $('#batchInput');
  batchInput.addEventListener('input', () => autosize(batchInput));
  batchInput.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      startBatch();
    }
  });

  // 直接贴在页面任意位置（不是输入框里）也能接住链接
  document.addEventListener('paste', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    const text = (e.clipboardData || window.clipboardData).getData('text');
    if (!text) return;
    const urls = extractUrls(text);
    if (!urls.length) return;
    e.preventDefault();
    setMode(urls.length > 1 ? 'batch' : 'single');
    const target = urls.length > 1 ? batchInput : urlInput;
    target.value = urls.join('\n');
    autosize(target);
    target.focus();
    toast(urls.length > 1 ? `已粘贴 ${urls.length} 条链接` : '已粘贴链接');
  });

  // ---- 文件
  $('#pickFileBtn').addEventListener('click', () => $('#fileInput').click());
  $('#fileInput').addEventListener('change', (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) setFile(f);
  });
  $('#clearFile').addEventListener('click', clearFile);

  // ---- 提交
  $('#startBtn').addEventListener('click', submitSingle);
  $('#batchStart').addEventListener('click', startBatch);
  $('#cancelBtn').addEventListener('click', cancelTask);
  $('#batchCancel').addEventListener('click', cancelBatch);

  // ---- 拖放：链接和文件都能接
  let dragDepth = 0;
  const hasPayload = (e) => {
    const dt = e.dataTransfer;
    if (!dt) return false;
    const types = Array.from(dt.types || []);
    return types.includes('Files') || types.includes('text/uri-list') || types.includes('text/plain');
  };
  document.addEventListener('dragenter', (e) => {
    if (!hasPayload(e)) return;
    e.preventDefault();
    dragDepth += 1;
    $('#dropOverlay').hidden = false;
  });
  document.addEventListener('dragover', (e) => {
    if (!hasPayload(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  document.addEventListener('dragleave', (e) => {
    if (!hasPayload(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) $('#dropOverlay').hidden = true;
  });
  document.addEventListener('drop', (e) => {
    if (!hasPayload(e)) return;
    e.preventDefault();
    dragDepth = 0;
    $('#dropOverlay').hidden = true;

    const files = e.dataTransfer.files;
    if (files && files.length) {
      setFile(files[0]);
      setMode('single');
      toast('文件已加入，点「开始提取」开始');
      return;
    }
    const text = e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain');
    const urls = extractUrls(text);
    if (!urls.length) { toast('没识别到链接'); return; }
    setMode(urls.length > 1 ? 'batch' : 'single');
    const target = urls.length > 1 ? $('#batchInput') : $('#urlInput');
    target.value = urls.join('\n');
    autosize(target);
    target.focus();
    toast(`已填入 ${urls.length} 条链接`);
  });

  // ---- 结果区
  $$('.tab').forEach((tab) => {
    tab.addEventListener('click', () => setTab(tab.dataset.tab));
  });

  let queryTimer = null;
  $('#searchInput').addEventListener('input', () => {
    clearTimeout(queryTimer);
    queryTimer = setTimeout(onQueryChange, 140);
  });
  $('#searchInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      jumpMatch(e.shiftKey ? -1 : 1);
    }
    if (e.key === 'Escape') {
      $('#searchInput').value = '';
      onQueryChange();
    }
  });
  $('#searchPrev').addEventListener('click', () => jumpMatch(-1));
  $('#searchNext').addEventListener('click', () => jumpMatch(1));

  $('#copyText').addEventListener('click', copyFullText);
  $$('[data-format]').forEach((btn) => {
    if (btn.closest('#batchList')) return;
    btn.addEventListener('click', () => doExport(btn.dataset.format));
  });

  // 分句：点时间戳复制单句，点文本多选，滚到底部增量渲染
  const segList = $('#segList');
  segList.addEventListener('click', async (e) => {
    const timeBtn = e.target.closest('[data-time]');
    if (timeBtn) {
      const s = state.filtered[Number(timeBtn.dataset.time)];
      if (!s) return;
      const ok = await copy(`[${fmtClock(s.start)}] ${s.text}`);
      toast(ok ? '已复制这一句' : '复制失败', ok ? '' : 'err');
      return;
    }
    const row = e.target.closest('.seg');
    if (!row) return;
    const k = Number(row.dataset.k);
    if (state.sel.has(k)) state.sel.delete(k);
    else state.sel.add(k);
    row.classList.toggle('sel', state.sel.has(k));
    updateSegStatus();
  });
  segList.addEventListener('scroll', () => {
    if (segList.scrollTop + segList.clientHeight >= segList.scrollHeight - 400) appendSegChunk();
  });
  segList.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && state.sel.size) copySegments(true);
  });

  $('#copyAllSegs').addEventListener('click', () => copySegments(false));
  $('#copySelectedSegs').addEventListener('click', () => copySegments(true));

  // ---- 改写
  $('#doRewrite').addEventListener('click', doRewrite);
  $('#copyRewrite').addEventListener('click', async () => {
    if (!state.rewriteText) { toast('还没有改写结果'); return; }
    const ok = await copy(state.rewriteText);
    toast(ok ? '已复制改写结果' : '复制失败', ok ? '' : 'err');
  });

  // ---- 批量
  $('#batchList').addEventListener('click', (e) => {
    const openBtn = e.target.closest('[data-open]');
    if (openBtn) { openTask(openBtn.dataset.open); return; }
    const expBtn = e.target.closest('[data-export]');
    if (expBtn) {
      const prev = state.taskId;
      state.taskId = expBtn.dataset.export;
      doExport(expBtn.dataset.format).finally(() => { state.taskId = prev; });
    }
  });
  $$('[data-batch-export]').forEach((btn) => {
    btn.addEventListener('click', () => batchExport(btn.dataset.batchExport));
  });

  // ---- 历史
  $('#history').addEventListener('click', (e) => {
    const del = e.target.closest('[data-del]');
    if (del) {
      e.stopPropagation();
      armDelete(del, del.dataset.del);
      return;
    }
    const openBtn = e.target.closest('[data-open]');
    if (openBtn) openTask(openBtn.dataset.open);
  });
  $('#refreshHistory').addEventListener('click', () => loadHistory(true));
  $('#histMore').addEventListener('click', () => {
    state.histLimit += 30;
    loadHistory(false);
  });

  $('#histFilters').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-status]');
    if (!btn) return;
    state.histStatus = btn.dataset.status;
    $$('#histFilters .chip').forEach((c) => c.classList.toggle('on', c === btn));
    loadHistory(true);
  });

  let histTimer = null;
  $('#histSearch').addEventListener('input', (e) => {
    clearTimeout(histTimer);
    histTimer = setTimeout(() => {
      state.histQuery = e.target.value.trim();
      loadHistory(true);
    }, 260);
  });

  // ---- 全局快捷键
  document.addEventListener('keydown', (e) => {
    const meta = e.metaKey || e.ctrlKey;

    if (meta && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      if ($('#paletteWrap').hidden) openPalette(); else closePalette();
      return;
    }

    if (!$('#paletteWrap').hidden) {
      if (e.key === 'Escape') { e.preventDefault(); closePalette(); return; }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const n = state.paletteItems.length;
        if (!n) return;
        state.paletteIndex = (state.paletteIndex + (e.key === 'ArrowDown' ? 1 : -1) + n) % n;
        renderPalette($('#paletteInput').value);
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        runCommand(state.paletteItems[state.paletteIndex]);
        return;
      }
    }

    if (e.key === 'Escape') {
      if (document.activeElement === $('#searchInput')) {
        $('#searchInput').value = '';
        onQueryChange();
      }
      return;
    }

    const tag = (e.target && e.target.tagName) || '';
    const typing = tag === 'INPUT' || tag === 'TEXTAREA' || (e.target && e.target.isContentEditable);

    if (meta && e.key === 'Enter') {
      e.preventDefault();
      if (state.mode === 'batch') startBatch(); else submitSingle();
      return;
    }

    // "/" 聚焦结果搜索：键盘优先
    if (!typing && e.key === '/') {
      if ($('#resultPanel').hidden) return;
      e.preventDefault();
      setTab(state.tab === 'rewrite' ? 'text' : state.tab);
      $('#searchInput').focus();
    }
  });

  // 系统主题变化：只有用户没手动选过才跟随
  if (window.matchMedia) {
    const mq = window.matchMedia('(prefers-color-scheme: light)');
    const onChange = (ev) => {
      let saved = null;
      try { saved = localStorage.getItem('c2t.theme'); } catch { /* 忽略 */ }
      if (saved === 'dark' || saved === 'light') return;
      setTheme(ev.matches ? 'light' : 'dark');
    };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }
}

/* ------------------------------------------------------------ 启动 */

function init() {
  syncThemeBtn();
  bind();
  autosize($('#urlInput'));
  $('#urlInput').focus();
  loadCapabilities();
  loadHistory();
}

init();
