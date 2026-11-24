/* ============================================================================
 * clip2text 前端
 *
 * 约束：原生 JS，无构建、无依赖、无 CDN。三个静态文件 clone 下来就能跑。
 * 主入口是「粘贴链接」；上传文件是次要入口，藏在粘贴框的下方。
 *
 * 结构：
 *   1. 工具函数     2. 状态     3. 主题     4. 能力探测
 *   5. 输入与提交   6. 任务进度  7. 结果渲染  8. 搜索 / 导出
 *   9. 改写         10. 内容解析 11. 批量     12. 历史
 *   13. 命令面板
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
  /* 内容解析：每个任务只有一份结果，后端会覆盖 */
  analysis: null,
  anaAvailable: false,
  /* 当前面板展示的是哪个任务：同一个任务切回来不重复打接口 */
  anaFor: null,
  /* idle | loading | empty | running | done | error */
  anaView: 'idle',
  anaRunning: false,
  /* 刚点过「重新解析」：这之后的一小段时间内读到 FAILED 不可信 */
  anaJustForced: false,
  /* 跳转序号：新的跳转让旧的「补正」回调自动失效 */
  jumpSeq: 0,
  settleTimer: null,
  redoTimer: null,
  hlTimer: null,
  batchId: null,
  batchTimer: null,
  batchTasks: [],
  histStatus: '',
  histQuery: '',
  histLimit: 30,
  histTotal: 0,
  paletteIndex: 0,
  paletteItems: [],
  /* 设置面板：当前分区、最近一次读到的配置/用量、接口错误（后端可能还没上线） */
  settingsTab: 'model',
  settings: null,
  settingsError: '',
  usage: null,
  usageError: '',
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

/*
 * POST 请求拿到的 text/event-stream 不能用 EventSource（它只发 GET），
 * 所以手写一个最小解析器：按空行切块，每块取 event: 和 data:。
 * 改写和解析共用这一份，避免两处各写一遍再各自长歪。
 *
 * onEvent(ev, payload) 抛异常 = 中断并把异常抛给调用方；
 * 返回 'stop' = 主动结束读取（已经拿到结果，不用再等）。
 */
async function readSSE(res, onEvent) {
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
      const dataMatch = block.match(/^data:\s*(.*)$/m);
      if (!dataMatch) continue;
      let payload;
      try { payload = JSON.parse(dataMatch[1]); } catch { continue; }
      const ev = evMatch ? evMatch[1] : 'message';
      const verdict = await onEvent(ev, payload);
      if (verdict === 'stop') return 'stopped';
    }
  }
  return 'done';
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

  /* 解析和改写共用一套 LLM 配置，但后端有独立开关，按它自己的字段判断 */
  if (caps.analysis && caps.analysis.available) pills.push(pill('内容解析', 'ok'));

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

  // LLM 未配置也要说一声。
  // 之前「解析」和「改写」在不可用时整个隐藏，用户既不知道有这个功能，
  // 也不知道差什么才能用 —— 等于功能不存在。现在改成显式告知。
  // 入口说明必须指向界面上的齿轮：写 .env 和「重启服务」正是上个迭代
  // 让人以为功能不存在的原因。
  if (!analysisAvailable(caps)) {
    items.push(noticeHtml('', 'spark',
      `<b>内容解析和 AI 改写未启用。</b>点右上角的齿轮，在「模型与 API Key」里填一个`
      + ` OpenAI 兼容服务（Base URL / Key / 模型名），或者只填 DeepSeek 的 Key，`
      + `保存后立即生效，不用重启。`
      + (analysisReason(caps) ? `<br><span class="hint">${esc(analysisReason(caps))}</span>` : '')));
  }

  box.innerHTML = items.join('');
  box.hidden = items.length === 0;
}

function ffReady(caps) {
  return !!(caps && caps.ffmpeg && caps.ffmpeg.available);
}

/* 解析和改写虽然是同一套 LLM 配置，但后端把它们拆成两个字段上报：
   契约里写的是 capabilities.analysis = {available, reason, source, model}。
   字段缺失（后端还是旧版本）一律当不可用，宁可少一个 tab 也不要点了报错。

   ⚠ 只认 available，不要自己去看 LLM_BASE_URL / caps.rewrite：
   后端支持 DeepSeek 兜底（只配 DEEPSEEK_API_KEY 也算可用），
   前端再去猜配置会让这批用户看不到功能。 */
function analysisAvailable(caps) {
  const c = caps || state.caps;
  return !!(c && c.analysis && c.analysis.available);
}

function analysisReason(caps) {
  const c = caps || state.caps;
  return (c && c.analysis && c.analysis.reason) || '未配置 LLM（LLM_BASE_URL / LLM_MODEL 或 DEEPSEEK_API_KEY）';
}

/* 当前用哪个模型在分析：llm = 自建/兼容端点，deepseek = 兜底 */
function analysisSourceLabel(caps) {
  const a = (caps || state.caps || {}).analysis;
  if (!a) return '';
  const model = a.model || '';
  if (a.source === 'deepseek') return model ? `DeepSeek 兜底 · ${model}` : 'DeepSeek 兜底';
  if (a.source === 'llm') return model ? `自配模型 · ${model}` : '自配模型';
  return model ? `模型 ${model}` : '';
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

    /* 解析 tab 常显。
       早期版本在不可用时把 tab 整个隐藏，结果是用户根本不知道有这个功能，
       更不知道该配什么才能用。现在改成：tab 一直在，进去后如实说明缺什么。 */
    state.anaAvailable = analysisAvailable(caps);
    $('#analysisTab').hidden = false;
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
    /* roving tabindex：只有选中的 tab 在 Tab 键序列里 */
    t.tabIndex = on ? 0 : -1;
  });
  $('#panelText').hidden = name !== 'text';
  $('#panelSegments').hidden = name !== 'segments';
  $('#panelAnalysis').hidden = name !== 'analysis';
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
  if (name === 'analysis') openAnalysis();
  /* 搜索只作用于正文和分句：改写结果和解析结果都不是「原文子串」，留着搜索框是误导 */
  $('#searchBox').style.visibility = (name === 'rewrite' || name === 'analysis') ? 'hidden' : '';
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

  // 解析同样要清：不同任务的摘要/章节混在一起是最糟的错误
  state.analysis = null;
  state.anaFor = null;
  state.anaRunning = false;
  state.anaView = 'idle';
  $('#anaBody').innerHTML = '';
  $('#anaState').className = 'ana-state';
  $('#anaState').textContent = '－';
  $('#anaMeta').textContent = '';
  showAnaProgress(false);
  anaButtons(false, false);
  $('#anaRedo').dataset.armed = '';

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

    // POST + SSE 不能用 EventSource，走公共解析器
    let finished = false;
    await readSSE(res, (ev, payload) => {
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
    });

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

/* ------------------------------------------------------------ 10. 内容解析 */

/*
 * 「解析」= 把长转写提取成结构（摘要 / 章节 / 要点 / 金句 / 实体），不重写原文。
 * 与「改写」的区别是**必须带时间戳**，且时间戳点了要能跳回分句里那一句 ——
 * 两小时播客几十万字，用户真正要的是「跳到 01:12:30 那儿听」。
 */

/* 解析面板顶部的状态行。kind: run / ok / err */
function anaBar(kind, label, meta) {
  const el = $('#anaState');
  el.className = 'ana-state' + (kind ? ' ' + kind : '');
  el.innerHTML = `<span class="dot" aria-hidden="true"></span>${esc(label)}`;
  $('#anaMeta').textContent = meta || '';
}

/* 面板固定展示「这份结果是谁分析的」，source/model 来自 capabilities.analysis */
function anaSourceLine() {
  const el = $('#anaSource');
  if (!el) return;
  const text = analysisSourceLabel();
  el.textContent = text ? `分析模型：${text}` : '';
  el.hidden = !text;
}

function anaButtons(running, hasResult) {
  const redo = $('#anaRedo');
  redo.hidden = !hasResult || !state.anaAvailable;
  redo.disabled = running;
  if (!running && redo.dataset.armed !== '1') {
    redo.innerHTML = '<svg class="ico" aria-hidden="true"><use href="#i-refresh"></use></svg>重新解析';
  }
  $('#anaCopyAll').hidden = !hasResult;
}

function setAnaView(view) {
  state.anaView = view;
  anaButtons(view === 'running', ['done', 'running', 'error'].includes(view));
}

function showAnaProgress(on, pct, text) {
  $('#anaProgress').hidden = !on;
  if (!on) return;
  const p = Math.max(0, Math.min(100, Math.round(pct)));
  $('#anaBar').style.width = p + '%';
  $('#anaProgressBar').setAttribute('aria-valuenow', String(p));
  $('#anaProgressText').textContent = text || '';
}

function anaHead(icon, title, count) {
  return `<div class="ana-section-head">
    <svg class="ico" aria-hidden="true"><use href="#i-${icon}"></use></svg>
    <h3>${esc(title)}</h3>${count != null ? `<span class="ana-count">${esc(count)}</span>` : ''}
  </div>`;
}

/* 时间戳按钮：解析面板里唯一有副作用的控件，可访问性必须到位 */
function anaTimeBtn(seconds, title) {
  const t = Number(seconds) || 0;
  return `<button class="ana-time" type="button" data-jump="${t}"
    title="${esc(title || '跳到这句')}" aria-label="跳到 ${fmtClock(t)} 这一句">${fmtClock(t)}</button>`;
}

function renderAnalysisEmpty() {
  anaBar('', '尚未解析');
  anaButtons(false, false);
  showAnaProgress(false);

  /* LLM 未配置：给出明确的开启方式，而不是一个点了会 400 的按钮。
     这比把 tab 藏起来友好得多 —— 藏起来等于功能不存在。 */
  if (!state.anaAvailable) {
    $('#anaBody').innerHTML = `<div class="ana-empty">
      <svg class="ico" aria-hidden="true"><use href="#i-spark"></use></svg>
      <p class="ana-empty-title">内容解析尚未启用</p>
      <p class="ana-empty-text">
        解析会把这份转写交给大模型，提取一句话摘要、带时间戳的章节、关键要点和金句。
        章节与金句的时间戳可以直接点，跳回对应的分句位置。
      </p>
      <div class="ana-setup">
        <p class="ana-setup-title">开启方式（二选一）</p>
        <p class="ana-setup-line">
          <b>方式一：</b>点右上角的齿轮，在「模型与 API Key」里填写任意 OpenAI 兼容服务
          <br><span class="hint">Base URL + API Key + 模型名，例如 https://api.deepseek.com 与 deepseek-chat；
          也可以指向本地 Ollama（http://localhost:11434/v1）</span>
        </p>
        <p class="ana-setup-line">
          <b>方式二：</b>在同一个面板里只填一个 DeepSeek 的 API Key
          <br><span class="hint">会自动复用为分析模型（deepseek-chat），与语音识别共用同一个 key</span>
        </p>
        <p class="ana-setup-line">保存后立即生效，<b>不用重启服务</b>。</p>
      </div>
      ${state.caps && state.caps.analysis && state.caps.analysis.reason
        ? `<p class="ana-empty-text hint">当前状态：${esc(state.caps.analysis.reason)}</p>` : ''}
    </div>`;
    return;
  }

  $('#anaBody').innerHTML = `<div class="ana-empty">
    <svg class="ico" aria-hidden="true"><use href="#i-scan"></use></svg>
    <p class="ana-empty-title">还没有解析结果</p>
    <p class="ana-empty-text">
      解析会把这份转写交给大模型，提取一句话摘要、带时间戳的章节、关键要点和金句。
      章节与金句的时间戳可以直接点，跳回对应的分句位置。
      <br>两小时播客、三万字的稿子要分窗处理，可能要等几十秒到两三分钟。
    </p>
    <div class="ana-empty-actions">
      <button class="btn btn-primary" type="button" data-ana-start="1">
        <svg class="ico" aria-hidden="true"><use href="#i-scan"></use></svg>开始解析
      </button>
    </div>
  </div>`;
}

function renderAnalysisError(msg, canRetry) {
  anaBar('err', '解析失败', '');
  /* 只有确实有旧结果时顶部才给「重新解析」；否则靠正文里的「重试」，
     两个按钮做同一件事会让人以为有区别 */
  anaButtons(false, !!state.analysis);
  showAnaProgress(false);
  $('#anaBody').innerHTML = `<div class="ana-error">
    <svg class="ico" aria-hidden="true"><use href="#i-alert"></use></svg>
    <div><b>解析没有完成。</b>${esc(msg || '没有拿到原因')}</div>
  </div>
  ${canRetry === false ? '' : `<div class="ana-empty-actions" style="margin-top:14px">
      <button class="btn btn-quiet btn-sm" type="button" data-ana-start="1">
        <svg class="ico" aria-hidden="true"><use href="#i-refresh"></use></svg>重试
      </button>
    </div>`}`;
}

function renderAnalysisFailed(a) {
  renderAnalysisError(a.error || '上一次解析失败了', true);
  anaBar('err', '解析失败', a.model ? `模型 ${a.model}` : '');
}

/* 把契约 §2 的 Analysis 渲染成卡片 */
function renderAnalysis(a) {
  const data = a || {};
  const chapters = Array.isArray(data.chapters) ? data.chapters : [];
  const points = Array.isArray(data.key_points) ? data.key_points : [];
  const quotes = Array.isArray(data.quotes) ? data.quotes : [];
  const entities = Array.isArray(data.entities) ? data.entities : [];

  const stamp = fmtWhen(data.created_at);
  const metaBits = [];
  if (data.model) metaBits.push(data.model);
  if (data.windows) metaBits.push(`${data.windows} 窗`);
  if (stamp) metaBits.push(stamp);
  anaBar('ok', '已解析', metaBits.join(' · '));
  anaButtons(false, true);
  showAnaProgress(false);

  const parts = [];

  if (data.abstract) {
    parts.push(`<p class="ana-abstract"><span class="ana-abstract-label">摘要</span>${esc(data.abstract)}</p>`);
  }

  if (chapters.length) {
    const rows = chapters.map((c) => {
      const start = Number(c.start) || 0;
      const end = Number(c.end) || 0;
      const range = end > start ? `${fmtClock(start)} – ${fmtClock(end)}` : fmtClock(start);
      return `<li class="ana-chapter">
        ${anaTimeBtn(start, `跳到 ${range}`)}
        <div class="ana-chapter-main">
          <p class="ana-chapter-title">${esc(c.title || '（无标题）')}</p>
          ${c.summary ? `<p class="ana-chapter-summary">${esc(c.summary)}</p>` : ''}
        </div>
      </li>`;
    }).join('');
    parts.push(`<section class="ana-section">
      ${anaHead('list', '章节', chapters.length)}
      <ol class="ana-chapters">${rows}</ol>
    </section>`);
  }

  if (points.length) {
    parts.push(`<section class="ana-section">
      ${anaHead('check', '关键要点', points.length)}
      <ul class="ana-points">${points.map((p) => `<li class="ana-point">${esc(p)}</li>`).join('')}</ul>
    </section>`);
  }

  if (quotes.length) {
    const rows = quotes.map((q, i) => {
      const start = Number(q.start) || 0;
      return `<li class="ana-quote">
        <p class="ana-quote-text">“${esc(q.text || '')}”</p>
        <div class="ana-quote-foot">
          ${anaTimeBtn(start, `跳到这句原话（${fmtClock(start)}）`)}
          ${q.note ? `<span class="ana-quote-note">${esc(q.note)}</span>` : '<span class="ana-quote-note"></span>'}
          <button class="ana-copy" type="button" data-copy-quote="${i}"
            title="复制这句原话" aria-label="复制这句原话">
            <svg class="ico" aria-hidden="true"><use href="#i-copy"></use></svg>
          </button>
        </div>
      </li>`;
    }).join('');
    parts.push(`<section class="ana-section">
      ${anaHead('quote', '金句', quotes.length)}
      <ul class="ana-quotes">${rows}</ul>
    </section>`);
  }

  if (entities.length) {
    parts.push(`<section class="ana-section">
      ${anaHead('tag', '实体', entities.length)}
      <ul class="ana-entities">${entities.map((e) => `<li class="ana-entity">${esc(e)}</li>`).join('')}</ul>
    </section>`);
  }

  if (!parts.length) {
    renderAnalysisError('模型返回了结果，但里面没有任何可展示的内容。', true);
    return;
  }

  const foot = [];
  if (data.language) foot.push(`语言 ${data.language}`);
  if (data.token_usage && (data.token_usage.prompt || data.token_usage.completion)) {
    const total = (Number(data.token_usage.prompt) || 0) + (Number(data.token_usage.completion) || 0);
    foot.push(`${total.toLocaleString('zh-CN')} tokens`);
  }
  if (data.windows) foot.push(`分 ${data.windows} 窗解析`);
  if (foot.length) {
    parts.push(`<div class="ana-foot">${foot.map(esc).join('<span class="sep">·</span>')}</div>`);
  }

  $('#anaBody').innerHTML = parts.join('');
  $('#anaBody').scrollTop = 0;
}

/* 打开解析 tab 时拉一次已有结果；同一个任务不重复打接口 */
async function openAnalysis() {
  const id = state.taskId;
  if (!id) return;
  anaSourceLine();
  if (state.anaFor === id && state.anaView !== 'idle') return;
  state.anaFor = id;

  /* LLM 未配置时不要去请求接口（必然 400），直接渲染配置指引。
     注意这里不能提前 return —— tab 现在常显，早退会导致面板一片空白，
     用户看到的只是一个「–」，比隐藏 tab 更让人困惑。 */
  if (!state.anaAvailable) {
    renderAnalysisEmpty();
    setAnaView('empty');
    return;
  }
  await loadAnalysis(id);
}

async function loadAnalysis(id, attempt) {
  const taskId = id || state.taskId;
  if (!taskId) return;
  const tries = attempt || 0;

  anaBar('', '读取中…', '');
  anaButtons(false, false);
  showAnaProgress(false);
  $('#anaBody').innerHTML = '<div class="ana-empty"><p class="ana-empty-text">正在读取已有解析…</p></div>';

  try {
    const res = await fetch(`${API}/tasks/${taskId}/analysis`);
    if (state.taskId !== taskId) return; // 期间切了任务

    /* 404 = 还没解析过，这是正常路径，不是错误 */
    if (res.status === 404) {
      state.analysis = null;
      renderAnalysisEmpty();
      setAnaView('empty');
      return;
    }
    if (!res.ok) {
      let detail = `读取失败（HTTP ${res.status}）`;
      try { detail = (await res.json()).detail || detail; } catch { /* 忽略 */ }
      throw new Error(detail);
    }

    const data = await res.json();
    if (state.taskId !== taskId) return;

    /*
     * 重新解析会把旧记录短暂置为 FAILED 再覆盖（后端要先落一个状态，
     * 否则中途崩掉会留下一个看不出问题的旧结果）。这个窗口非常短，
     * 读到 FAILED 不代表真的失败 —— 隔一下重读，别给用户弹一个假错误。
     */
    if (data.state === 'FAILED' && tries < 2 && (state.anaRunning || state.anaJustForced)) {
      await new Promise((r) => setTimeout(r, 700));
      if (state.taskId !== taskId) return;
      return loadAnalysis(taskId, tries + 1);
    }

    state.analysis = data;
    if (data.state === 'FAILED') {
      renderAnalysisFailed(data);
      setAnaView('error');
    } else {
      renderAnalysis(data);
      setAnaView('done');
    }
  } catch (err) {
    if (state.taskId !== taskId) return;
    renderAnalysisError(err.message);
    setAnaView('error');
  }
}

/* 进度条百分比：后端只给 done/total，这里把两个阶段摊到 0-100 上，
   否则 reducing 一开始会从 0% 重来，看起来像失败重跑。 */
function anaPct(phase, done, total) {
  const d = Number(done) || 0;
  const t = Number(total) || 0;
  const ratio = t > 0 ? Math.max(0, Math.min(1, d / t)) : 0;
  if (phase === 'reducing') return 90 + ratio * 9;
  if (phase === 'done') return 100;
  return ratio * 90;
}

async function startAnalyze(force) {
  const id = state.taskId;
  if (!id) { toast('没有可解析的任务'); return; }
  if (state.anaRunning) return;
  if (!state.anaAvailable) { toast(analysisReason(), 'err'); return; }

  state.anaRunning = true;
  state.anaFor = id;
  state.anaJustForced = !!force;
  setAnaView('running');
  anaBar('run', '解析中…', '');
  anaSourceLine();
  showAnaProgress(true, 0, '正在连接模型…');
  $('#anaBody').innerHTML = '<div class="ana-empty"><p class="ana-empty-text">正在分析，长内容要分窗处理，请稍候…</p></div>';

  let gotResult = false;

  try {
    const res = await fetch(`${API}/tasks/${id}/analyze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: !!force }),
    });

    if (!res.ok) {
      let detail = `请求失败（HTTP ${res.status}）`;
      try { detail = (await res.json()).detail || detail; } catch { /* 忽略 */ }
      throw new Error(detail);
    }

    /* 契约 5.1：已有结果且 force=false 时后端直接回 JSON，不发 SSE */
    const ctype = res.headers.get('content-type') || '';
    if (ctype.includes('application/json')) {
      const data = await res.json();
      if (state.taskId !== id) return;
      state.analysis = data;
      renderAnalysis(data);
      setAnaView('done');
      toast('已有解析结果');
      return;
    }

    await readSSE(res, (ev, payload) => {
      if (state.taskId !== id) return 'stop'; // 用户切走了，别再改 DOM

      if (ev === 'progress') {
        const phase = payload.phase || 'mapping';
        const done = Number(payload.done) || 0;
        const total = Number(payload.total) || 0;
        /*
         * 后端 message 是最具体的（「正在分析第 6/8 段」），优先用它。
         * 不要再拼一句自己算的「正在分析 5/8 段」—— done 是「已完成数」，
         * 和 message 里的「第 6 段」并排会出现 5/8 与 6/8 打架的观感。
         */
        let label = payload.message;
        if (!label) {
          if (phase === 'reducing') label = '正在汇总';
          else if (total) label = `正在分析 ${done}/${total} 段`;
          else label = '正在分析';
        }
        showAnaProgress(true, anaPct(phase, done, total), label);
        anaBar('run', '解析中…', total ? `${done}/${total}` : '');
      } else if (ev === 'result') {
        gotResult = true;
        state.analysis = payload;
        if (payload && payload.state === 'FAILED') {
          renderAnalysisFailed(payload);
          setAnaView('error');
        } else {
          renderAnalysis(payload);
          setAnaView('done');
        }
      } else if (ev === 'error') {
        throw new Error(payload.detail || '解析失败');
      }
      return undefined;
    });

    if (state.taskId !== id) return;
    if (gotResult) {
      showAnaProgress(true, 100, '完成');
      setTimeout(() => {
        if (state.anaRunning) return;
        showAnaProgress(false);
      }, 900);
      toast('解析完成');
    } else {
      throw new Error('连接结束了，但没有收到解析结果');
    }
  } catch (err) {
    if (state.taskId !== id) return;
    renderAnalysisError(err.message);
    setAnaView('error');
    toast(err.message, 'err');
  } finally {
    if (state.taskId === id) {
      state.anaRunning = false;
      anaButtons(false, !!state.analysis);
      if (!gotResult) showAnaProgress(false);
    }
    /* 结果一落地，那个「FAILED 短暂窗口」就结束了 */
    setTimeout(() => { state.anaJustForced = false; }, 1200);
  }
}

/* 「重新解析」二次确认：沿用删除任务那套「点一次变成确认」的做法，
   不用 confirm()（原生弹窗会阻塞页面，也不能定制样式）。 */
let anaRedoOriginal = '';

function armReanalyze(btn) {
  if (!btn || state.anaRunning) return;
  if (btn.dataset.armed === '1') {
    btn.dataset.armed = '';
    btn.classList.remove('btn-danger');
    btn.innerHTML = anaRedoOriginal || '重新解析';
    startAnalyze(true);
    return;
  }
  anaRedoOriginal = btn.innerHTML;
  btn.dataset.armed = '1';
  btn.classList.add('btn-danger');
  btn.innerHTML = '<span style="font-size:11.5px">确认重新解析</span>';
  btn.title = '再点一次会覆盖当前结果，并重新调用模型';
  clearTimeout(state.redoTimer);
  state.redoTimer = setTimeout(() => {
    if (!btn.isConnected || btn.dataset.armed !== '1') return;
    btn.innerHTML = anaRedoOriginal || '重新解析';
    btn.classList.remove('btn-danger');
    btn.dataset.armed = '';
    btn.title = '重新解析（会调用模型，覆盖当前结果）';
  }, 3200);
}

/*
 * 把某一行滚到分句列表的中间。
 *
 * 不能只算一次 scrollTop：.seg 有 content-visibility:auto，屏幕外的行按
 * contain-intrinsic-size 的 46px 估算，一旦滚过去，真实高度替换估算值，
 * 总高度和偏移都会变 —— 一次到位的计算会落在错的地方。
 * 所以按「量→挪→重量」迭代收敛，每次 getBoundingClientRect 都强制重排。
 */
function centerRowInList(list, row) {
  for (let i = 0; i < 12; i += 1) {
    const lr = list.getBoundingClientRect();
    const rr = row.getBoundingClientRect();
    const delta = (rr.top - lr.top) - (list.clientHeight / 2) + (rr.height / 2);
    if (Math.abs(delta) < 2) return true;
    const max = Math.max(0, list.scrollHeight - list.clientHeight);
    const next = Math.max(0, Math.min(max, list.scrollTop + delta));
    if (next === list.scrollTop) return false; // 到底了，挪不动
    list.scrollTop = next;
  }
  return false;
}

/*
 * 定位之后还得「跟一下」。
 *
 * 实测：某些情况下（尤其是刚从别的 tab 切过来）浏览器会在下一帧才把
 * content-visibility 的估算高度换成真实高度，导致已经居中的行又漂走几十到几百像素。
 * 所以在随后几帧和稍晚一点各补一次居中；用户一旦自己滚动就立刻停手。
 */
function settleRowCenter(list, row) {
  const token = (state.jumpSeq = (state.jumpSeq || 0) + 1);
  const mine = token;
  let userScrolled = false;
  const onUserScroll = () => { userScrolled = true; };
  list.addEventListener('wheel', onUserScroll, { passive: true, once: true });
  list.addEventListener('touchstart', onUserScroll, { passive: true, once: true });

  const refit = () => {
    if (mine !== state.jumpSeq) return; // 又跳了一次，交给新的那次
    if (userScrolled) return;
    if (!row.isConnected) return;
    centerRowInList(list, row);
  };

  requestAnimationFrame(() => {
    refit();
    requestAnimationFrame(refit);
  });
  clearTimeout(state.settleTimer);
  state.settleTimer = setTimeout(refit, 120);
  setTimeout(refit, 360);
}

/*
 * 时间戳跳转 —— 这是整个功能的核心。
 * 难点：分句是分批增量渲染的（每批 SEG_CHUNK 条），两小时播客有几千句，
 * 目标句很可能还没进 DOM，所以必须先把列表补渲染到目标行再滚动。
 */
function revealSegmentAt(seconds) {
  const t = Math.max(0, Number(seconds) || 0);
  const all = state.segAll;

  if (!all || !all.length) {
    toast('这个任务没有带时间戳的分句，跳不过去', 'err');
    return;
  }

  /* 搜索态下列表里只有命中的句子，目标句大概率不在，先清掉筛选 */
  if (state.query) {
    state.query = '';
    $('#searchInput').value = '';
    setSearchCount('', false);
  }

  /* 先切 tab 再重建列表：面板不可见时量出来的高度全是 0 */
  setTab('segments');
  state.segDirty = false;
  renderSegments();

  /* 找「这个时刻正在播的那一句」：第一条 end 越过 t 的分句 */
  let idx = all.findIndex((s) => (Number(s.end) || 0) > t);
  if (idx < 0) idx = all.length - 1;

  /* 补渲染：appendSegChunk 一次 SEG_CHUNK 条，循环到目标行出现为止 */
  let guard = 0;
  while (state.segRendered <= idx && guard < 500) {
    appendSegChunk();
    guard += 1;
  }

  const row = $(`#segList .seg[data-k="${idx}"]`);
  if (!row) {
    toast('没找到对应的分句', 'err');
    return;
  }

  /* 只滚分句那个内层容器：用 scrollIntoView 会把整页一起滚走 */
  const list = $('#segList');
  centerRowInList(list, row);
  settleRowCenter(list, row);

  /* 高亮 2 秒，够看清是哪一句 */
  list.querySelectorAll('.seg.flash').forEach((el) => el.classList.remove('flash'));
  row.classList.add('flash');
  clearTimeout(state.hlTimer);
  state.hlTimer = setTimeout(() => row.classList.remove('flash'), 2000);

  const s = all[idx];
  toast(`已跳到 ${fmtClock(s.start)}`);
}

/* 把整份解析导成 Markdown，方便用户粘进笔记 */
function analysisMarkdown() {
  const a = state.analysis;
  if (!a) return '';
  const out = [];
  if (a.abstract) out.push(`## 摘要\n\n${a.abstract}`);
  const chapters = Array.isArray(a.chapters) ? a.chapters : [];
  if (chapters.length) {
    out.push('## 章节');
    for (const c of chapters) {
      out.push(`- **[${fmtClock(c.start)}]** ${c.title || ''}${c.summary ? `\n  ${c.summary}` : ''}`);
    }
  }
  const points = Array.isArray(a.key_points) ? a.key_points : [];
  if (points.length) {
    out.push('## 关键要点');
    for (const p of points) out.push(`- ${p}`);
  }
  const quotes = Array.isArray(a.quotes) ? a.quotes : [];
  if (quotes.length) {
    out.push('## 金句');
    for (const q of quotes) out.push(`> ${q.text}${q.note ? `\n>\n> —— ${q.note}` : ''}\n\n时间 ${fmtClock(q.start)}`);
  }
  const entities = Array.isArray(a.entities) ? a.entities : [];
  if (entities.length) out.push(`## 实体\n\n${entities.join(' · ')}`);
  return out.join('\n\n');
}

/* ------------------------------------------------------------ 11. 批量 */

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

/* ------------------------------------------------------------ 12. 历史 */

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

/* -------------------------------------------------------- 13. 命令面板 */

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
    id: 'settings',
    label: '设置：API Key 与用量',
    icon: 'setting',
    run: () => openSettings(),
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

/* ------------------------------------------------- 14. 设置：Key 与用量 */

/*
 * 为什么要有这块：
 * 上一个迭代里，内容解析和 AI 改写要配 LLM 才能用，而配置只能改 .env 再重启，
 * 普通用户根本走不到那一步 —— 在他们眼里功能就是不存在。
 * 所以配置入口必须开在界面上：填完保存，立即生效，不用碰 .env、不用重启。
 *
 * 几条不可动摇的约束（见 docs/SETTINGS-CONTRACT.md）：
 *   · 后端 GET 只返回掩码，前端也永远不把 key 原文写进 DOM 的常驻属性里
 *   · Key 输入框留空保存 = 不修改（后端约定传空串即保持）
 *   · Key 输入框 autocomplete="new-password"，别让浏览器密码管理器收走
 *   · 错误必须显示在表单下方，不能只闪一个 toast
 */

const KIND_LABEL = {
  asr: '语音识别',
  ocr: '图片识别',
  analysis: '内容解析',
  rewrite: 'AI 改写',
};

/* 用量区展示多少天的条形。契约里 by_day 是 30 天，但 30 根条在一个弹层里太挤。 */
const USAGE_BARS = 7;

function fmtMoney(n) {
  const v = Number(n) || 0;
  if (!v) return '0.00';
  if (v < 0.01) return v.toFixed(4);
  return v.toFixed(2);
}

function fmtTok(n) {
  const v = Math.round(Number(n) || 0);
  return v.toLocaleString('en-US');
}

/* 音频时长写成 2h06m / 12m30s，别用 00:12:30 —— 在用量语境下更好读 */
function fmtAudio(seconds) {
  const t = Math.max(0, Math.round(Number(seconds) || 0));
  if (!t) return '0';
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  if (h) return `${h}h${String(m).padStart(2, '0')}m`;
  if (m) return `${m}m${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

/* 一行用量的「数量」列：每种 kind 关注的计量单位不一样 */
function usageAmount(kind, b) {
  const bucket = b || {};
  if (kind === 'asr') return fmtAudio(bucket.audio_seconds);
  if (kind === 'ocr') return `${Math.round(Number(bucket.images) || 0)} 张`;
  const tok = (Number(bucket.prompt_tokens) || 0) + (Number(bucket.completion_tokens) || 0);
  return tok ? `${fmtTok(tok)} tokens` : '—';
}

function showFormError(sel, msg) {
  const el = $(sel);
  if (!el) return;
  if (!msg) { el.hidden = true; el.textContent = ''; return; }
  el.textContent = String(msg);
  el.hidden = false;
}

/* ---- 打开 / 关闭：锁定页面滚动 + 焦点管理 ---- */

function openSettings(tab) {
  const wrap = $('#settingsWrap');
  if (!wrap || !wrap.hidden) {
    if (!wrap.hidden && tab) setSettingsTab(tab);
    return;
  }
  wrap.hidden = false;
  document.body.classList.add('settings-open');
  $('#settingsBtn').setAttribute('aria-expanded', 'true');

  if (tab) setSettingsTab(tab);
  else setSettingsTab(state.settingsTab || 'model');

  /* 面板一打开就去拉：不能等用户点，否则「已配置 / 未配置」是空白的 */
  loadSettings();
  loadUsage();

  const first = $('#sBaseUrl');
  if (first && !first.closest('[hidden]')) first.focus();
  else $('#settingsDialog').focus();
}

function closeSettings() {
  const wrap = $('#settingsWrap');
  if (!wrap || wrap.hidden) return;
  wrap.hidden = true;
  document.body.classList.remove('settings-open');
  $('#settingsBtn').setAttribute('aria-expanded', 'false');
  /* 焦点一律还到打开它的齿轮上。
     不还的话，弹层一关焦点就掉到 body，键盘用户得从页首重新 Tab 一遍；
     还到「关闭前聚焦的元素」听着更聪明，但那个元素多半在已隐藏的弹层里，
     或者是个和这次操作无关的输入框 —— 还到触发按钮才是可预期的。 */
  $('#settingsBtn').focus();
}

function setSettingsTab(name) {
  const which = name === 'usage' ? 'usage' : 'model';
  state.settingsTab = which;
  $$('.stab').forEach((b) => {
    const on = b.dataset.stab === which;
    b.classList.toggle('on', on);
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
  });
  $('#sviewModel').hidden = which !== 'model';
  $('#sviewUsage').hidden = which !== 'usage';
  if (which === 'usage') {
    /* 切到用量时按需再拉一次：用户可能刚跑完一个任务 */
    if (!state.usage) loadUsage();
  }
}

/* 焦点陷阱：设置面板是 aria-modal，Tab 不能跑到背后的页面上。
   不做的话，键盘用户在弹层里 Tab 几下就「消失」到页面里了。 */
function trapSettingsFocus(e) {
  if (e.key !== 'Tab') return;
  const box = $('#settingsDialog');
  if (!box) return;
  const focusables = $$(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), '
    + 'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    box,
  ).filter((el) => el.offsetParent !== null || el === document.activeElement);
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  } else if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  }
}

/* ---- 读配置 ---- */

async function loadSettings() {
  try {
    const view = await api('/settings');
    state.settings = view;
    state.settingsError = '';
    renderSettings(view);
  } catch (err) {
    state.settings = null;
    /* 后端还没实现（404）也得能开面板：明确告诉用户接口不可用，
       按钮禁用，而不是让整个面板炸掉、或者假装保存成功了。 */
    state.settingsError = err.message;
    renderSettings(null, err.message);
  }
}

function sourceLabel(src) {
  if (src === 'settings') return '界面配置';
  if (src === 'env') return '来自环境变量';
  return '';
}

function renderSettings(view, errMsg) {
  const banner = $('#sModelState');
  const llm = (view && view.llm) || {};
  const ds = (view && view.deepseek) || {};

  /* Base URL / 模型名可以回填（不是秘密）；Key 只回填空值 + 掩码 placeholder */
  $('#sBaseUrl').value = llm.base_url || '';
  $('#sModel').value = llm.model || '';
  $('#sApiKey').value = '';
  $('#sDsKey').value = '';
  $('#sApiKey').type = 'password';
  $('#sDsKey').type = 'password';
  resetEye('#sApiKeyEye');
  resetEye('#sDsKeyEye');

  if (llm.api_key_set && llm.api_key_masked) {
    $('#sApiKey').placeholder = llm.api_key_masked;
    $('#sApiKeyHint').textContent = '已保存，留空保存 = 不修改。要替换就填入新的 Key。';
  } else {
    $('#sApiKey').placeholder = 'sk-…';
    $('#sApiKeyHint').textContent = '还没有配置 Key。';
  }
  if (ds.api_key_set && ds.api_key_masked) {
    $('#sDsKey').placeholder = ds.api_key_masked;
    $('#sDsKeyHint').textContent = '已保存，留空保存 = 不修改。要替换就填入新的 Key。';
  } else {
    $('#sDsKey').placeholder = 'sk-…';
    $('#sDsKeyHint').textContent = '还没有配置 Key。';
  }

  /* 来源标签：来自 env 时必须说清楚「你在界面上改会覆盖它」 */
  const llmTag = $('#sLlmSource');
  const dsTag = $('#sDsSource');
  const ls = sourceLabel(llm.source);
  const dst = sourceLabel(ds.source);
  llmTag.hidden = !ls;
  llmTag.textContent = ls;
  llmTag.classList.toggle('env', llm.source === 'env');
  dsTag.hidden = !dst;
  dsTag.textContent = dst;
  dsTag.classList.toggle('env', ds.source === 'env');

  /* 配置状态的横幅 */
  banner.hidden = false;
  if (errMsg) {
    /* 404 是最常见的一种（后端还没部署到这个版本），别把 "Not Found" 原文
       甩给用户。其他错误照实显示，诊断信息对报 bug 有用。 */
    const notFound = /404|Not Found/i.test(errMsg);
    banner.className = 's-banner bad';
    banner.innerHTML = '<svg class="ico" aria-hidden="true"><use href="#i-alert"></use></svg>'
      + `<div><b>设置接口暂时不可用。</b>${notFound ? '这个版本的后端还没有配置接口（404）。' : esc(errMsg)}<br>`
      + '<span class="s-banner-sub">界面已就绪，等后端上线后无需改动；'
      + '这段时间仍然可以用 <code>.env</code> 配好再重启。</span></div>';
    setSettingsEnabled(false);
  } else {
    setSettingsEnabled(true);
    const ready = !!(view && view.analysis_ready);
    if (ready) {
      /* 这里拼的是纯文本片段，统一在最后 esc 一次。
         别在片段里先 esc 一遍再整体 esc —— 模型名里带 & 会变成 &amp;amp;。 */
      const parts = [];
      if (llm.api_key_set) {
        parts.push(`OpenAI 兼容服务${llm.model ? ` · ${llm.model}` : ''}`
          + (llm.source === 'env' ? '（来自环境变量，保存后会覆盖它）' : ''));
      }
      if (ds.api_key_set) {
        parts.push(`DeepSeek 兜底${ds.source === 'env' ? '（来自环境变量）' : ''}`);
      }
      if (!parts.length) parts.push('已从环境变量读取配置');
      banner.className = 's-banner';
      banner.innerHTML = '<svg class="ico" aria-hidden="true"><use href="#i-check"></use></svg>'
        + `<div><b>已配置好，内容解析与 AI 改写可用。</b>${esc(parts.join('；'))}。</div>`;
    } else {
      banner.className = 's-banner warn';
      banner.innerHTML = '<svg class="ico" aria-hidden="true"><use href="#i-alert"></use></svg>'
        + '<div><b>还没配置。</b>下面两种方式<b>任选一个</b>即可：填一个 OpenAI 兼容服务，'
        + '或只填 DeepSeek 的 Key。</div>';
    }
  }

  const path = $('#sConfigPath');
  if (view && view.config_path) {
    path.hidden = false;
    path.textContent = `配置文件：${view.config_path}`;
  } else {
    path.hidden = true;
  }
}

/* 接口不可用时把会写数据的按钮全禁掉，避免用户点了以为成功 */
function setSettingsEnabled(on) {
  ['#sLlmSave', '#sLlmClear', '#sDsSave', '#sDsClear', '#sTestBtn', '#uPricingSave']
    .forEach((sel) => {
      const el = $(sel);
      if (el) el.disabled = !on;
    });
}

function resetEye(sel) {
  const btn = $(sel);
  if (!btn) return;
  btn.setAttribute('aria-pressed', 'false');
  btn.setAttribute('aria-label', '显示 Key');
  const use = btn.querySelector('use');
  if (use) use.setAttribute('href', '#i-eye');
}

/* ---- 显示 / 隐藏 Key ---- */

function toggleKeyVisible(inputSel, btnSel) {
  const input = $(inputSel);
  const btn = $(btnSel);
  if (!input || !btn) return;
  const showing = input.type === 'password';
  input.type = showing ? 'text' : 'password';
  btn.setAttribute('aria-pressed', String(showing));
  btn.setAttribute('aria-label', showing ? '隐藏 Key' : '显示 Key');
  const use = btn.querySelector('use');
  if (use) use.setAttribute('href', showing ? '#i-eye-off' : '#i-eye');
}

/* ---- 保存 / 清除 ---- */

async function saveLLM() {
  showFormError('#sLlmError', '');
  const baseUrl = $('#sBaseUrl').value.trim();
  const key = $('#sApiKey').value.trim();
  const model = $('#sModel').value.trim();

  if (!baseUrl) { showFormError('#sLlmError', 'Base URL 不能为空，例如 https://api.deepseek.com'); $('#sBaseUrl').focus(); return; }
  if (!model) { showFormError('#sLlmError', '模型名不能为空，例如 deepseek-chat'); $('#sModel').focus(); return; }

  const body = { base_url: baseUrl, model };
  /* 留空 = 不修改。契约：传空串或不传都表示保持原值。 */
  if (key) body.api_key = key;

  const btn = $('#sLlmSave');
  btn.disabled = true;
  try {
    const view = await api('/settings/llm', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    state.settings = view;
    renderSettings(view);
    toast('已生效');
    /* tab 的可用状态来自 capabilities，必须重新拉一次，
       否则用户填完了返回页面，解析 tab 还是「未启用」 */
    await loadCapabilities();
    if (state.settingsTab === 'usage') loadUsage();
  } catch (err) {
    showFormError('#sLlmError', err.message);
    toast('保存失败', 'err');
  } finally {
    btn.disabled = false;
  }
}

/* 清除走「点一次变确认」，和删除任务那套一致：不用 confirm() */
const armed = { llm: null, ds: null };

function armClear(which) {
  const btn = which === 'llm' ? $('#sLlmClear') : $('#sDsClear');
  if (!btn) return;
  const original = '清除';
  if (btn.dataset.armed === '1') {
    btn.dataset.armed = '';
    btn.classList.remove('btn-danger');
    btn.textContent = original;
    if (which === 'llm') clearLLM(); else clearDeepseek();
    return;
  }
  btn.dataset.armed = '1';
  btn.classList.add('btn-danger');
  btn.textContent = '确认清除？';
  clearTimeout(armed[which]);
  armed[which] = setTimeout(() => {
    if (!btn.isConnected || btn.dataset.armed !== '1') return;
    btn.textContent = original;
    btn.classList.remove('btn-danger');
    btn.dataset.armed = '';
  }, 3200);
}

async function clearLLM() {
  showFormError('#sLlmError', '');
  const btn = $('#sLlmClear');
  btn.disabled = true;
  try {
    const view = await api('/settings/llm', { method: 'DELETE' });
    state.settings = view;
    renderSettings(view);
    toast('已清除');
    await loadCapabilities();
  } catch (err) {
    showFormError('#sLlmError', err.message);
    toast('清除失败', 'err');
  } finally {
    btn.disabled = false;
  }
}

async function saveDeepseek() {
  showFormError('#sDsError', '');
  const key = $('#sDsKey').value.trim();
  if (!key) {
    showFormError('#sDsError', '请填入 DeepSeek 的 API Key。（留空保存不会修改已保存的值）');
    $('#sDsKey').focus();
    return;
  }
  const btn = $('#sDsSave');
  btn.disabled = true;
  try {
    const view = await api('/settings/deepseek', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: key }),
    });
    state.settings = view;
    renderSettings(view);
    toast('已生效');
    await loadCapabilities();
  } catch (err) {
    showFormError('#sDsError', err.message);
    toast('保存失败', 'err');
  } finally {
    btn.disabled = false;
  }
}

async function clearDeepseek() {
  showFormError('#sDsError', '');
  const btn = $('#sDsClear');
  btn.disabled = true;
  try {
    /* 优先 DELETE。契约只写了 PUT，但后端后来补了 DELETE /settings/deepseek；
       先用 DELETE，404 再回退成 PUT 空 key —— 而 PUT 空 key 在后端是「不修改」，
       所以那条路只能算降级，成功了也要说清楚。 */
    let view = null;
    try {
      view = await api('/settings/deepseek', { method: 'DELETE' });
    } catch (err) {
      if (!/404|Not Found/i.test(err.message)) throw err;
      view = await api('/settings/deepseek', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: '' }),
      });
    }
    state.settings = view;
    renderSettings(view);
    toast('已清除');
    await loadCapabilities();
  } catch (err) {
    showFormError('#sDsError', err.message);
  } finally {
    btn.disabled = false;
  }
}

/* 后端会把上游的原始报错原样塞进 detail，里面常常是几百字的 JSON。
   直接把整段贴出来会把面板撑爆、且用户只需要那句 "key 无效"。
   能从 JSON 里抠出 message 就只显示 message，抠不出就截断。 */
function prettyDetail(text) {
  const raw = String(text ?? '').trim();
  if (!raw) return '';
  const m = raw.match(/"message"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  let msg = m ? m[1].replace(/\\"/g, '"').replace(/\\n/g, ' ') : raw.replace(/\s+/g, ' ');
  if (msg.length > 180) msg = msg.slice(0, 180) + '…';
  /* 上游的鉴权报错里会回显 key 的尾部（****wxyz）。那不是我们泄露的，
     但也没必要留在屏幕上，收一下。 */
  return msg.replace(/\*{2,}[A-Za-z0-9]{2,}/g, '****');
}

/* 一个 Key 常能调多个模型，用户记不住名字。
   这里用当前填的 base_url + key 去拉 GET /v1/models，
   拉到之后既填 datalist（输入框下拉），也渲染成可点选列表。 */
async function fetchModels() {
  const btn = $('#sModelFetch');
  const box = $('#sModels');
  const baseUrl = ($('#sBaseUrl').value || '').trim();
  const apiKey = ($('#sApiKey').value || '').trim();

  if (!baseUrl) {
    showLlmError('请先填写 Base URL');
    return;
  }

  btn.disabled = true;
  const oldText = btn.innerHTML;
  btn.textContent = '获取中…';
  box.hidden = false;
  box.innerHTML = '<p class="s-models-empty">正在向服务端拉取模型列表…</p>';

  try {
    const res = await fetch(`${API}/settings/models`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // key 留空时后端会回落到已保存的那个，所以用户不重填也能拉
      body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }),
    });
    const data = await res.json();

    if (!res.ok || !data.ok) {
      const reason = (data && data.reason) || `获取失败（HTTP ${res.status}）`;
      box.innerHTML = `<p class="s-models-empty s-models-err">${esc(reason)}</p>`;
      return;
    }

    const models = data.models || [];
    if (!models.length) {
      box.innerHTML = '<p class="s-models-empty">这条 Key 下没有可用模型，请手工填写模型名。</p>';
      return;
    }

    // datalist 让输入框本身就能下拉选
    $('#sModelList').innerHTML = models
      .map((m) => `<option value="${esc(m.id)}">${esc(m.name || m.id)}</option>`)
      .join('');

    const current = ($('#sModel').value || '').trim() || data.current || '';
    const chatCount = models.filter((m) => m.chat_capable !== false).length;

    box.innerHTML = `<p class="s-models-title">这条 Key 能调 ${models.length} 个模型`
      + `（其中 ${chatCount} 个可用于对话），点一个填入：</p>`
      + models.map((m) => {
        const disabled = m.chat_capable === false;
        const bits = [];
        if (m.context_window) bits.push(`上下文 ${fmtTokens(m.context_window)}`);
        if (m.max_output_tokens) bits.push(`输出 ${fmtTokens(m.max_output_tokens)}`);
        if (disabled) bits.push('不能用于对话');
        const active = m.id === current ? ' on' : '';
        return `<button type="button" class="s-model-chip${active}" data-model="${esc(m.id)}"`
          + `${disabled ? ' data-nonchat="1"' : ''} title="${esc(m.id)}">`
          + `<span class="s-model-id">${esc(m.id)}</span>`
          + (bits.length ? `<span class="s-model-meta">${esc(bits.join(' · '))}</span>` : '')
          + `</button>`;
      }).join('');
  } catch (err) {
    box.innerHTML = `<p class="s-models-empty s-models-err">${esc(err.message)}</p>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = oldText;
  }
}

/* 128000 -> 128K，便于在窄条里显示 */
function fmtTokens(n) {
  const num = Number(n) || 0;
  if (num >= 1000000) return `${(num / 1000000).toFixed(num % 1000000 ? 1 : 0)}M`;
  if (num >= 1000) return `${Math.round(num / 1000)}K`;
  return String(num);
}

async function testConnection() {
  const out = $('#sTestOut');
  const hint = $('#sTestHint');
  out.className = 's-test-out';
  out.textContent = '测试中…';
  hint.hidden = true;
  hint.textContent = '';
  try {
    const res = await api('/settings/test', { method: 'POST' });
    const ok = !!(res && (res.ok || res.success || res.available));
    out.className = 's-test-out ' + (ok ? 'ok' : 'bad');
    out.textContent = ok ? `连接成功${res.model ? ' · ' + res.model : ''}` : '连接失败';
    if (!ok && res && res.detail) {
      out.textContent = '连接失败';
      hint.textContent = prettyDetail(res.detail);
      hint.hidden = false;
    }
  } catch (err) {
    /* 后端可能压根没实现这个端点（404）—— 这是可选项，不能因此报错吓人 */
    const notFound = /404|Not Found/i.test(err.message);
    out.className = 's-test-out';
    out.textContent = notFound ? '这个版本后端没有提供测试接口' : '测试失败';
    hint.textContent = notFound
      ? '可以保存后直接跑一次解析，用真实调用验证。'
      : prettyDetail(err.message);
    hint.hidden = false;
  }
}

/* ---- 用量 ---- */

async function loadUsage() {
  try {
    const data = await api('/usage?days=30');
    state.usage = data;
    state.usageError = '';
    renderUsage(data);
  } catch (err) {
    state.usage = null;
    state.usageError = err.message;
    renderUsage(null, err.message);
  }
}

function renderUsage(data, errMsg) {
  const empty = $('#uEmpty');
  const sections = $('#uSections');
  const totalCost = $('#uTotalCost');
  const totalSub = $('#uTotalSub');
  const pricing = $('.u-pricing');

  if (errMsg) {
    empty.hidden = false;
    sections.hidden = true;
    totalCost.textContent = '—';
    totalSub.textContent = '用量接口暂时不可用';
    empty.querySelector('p').textContent = '读不到用量';
    empty.querySelector('span').textContent = '后端可能还没部署用量接口；不影响配置和使用。';
    if (pricing) pricing.hidden = true;
    return;
  }

  if (pricing) pricing.hidden = false;
  empty.querySelector('p').textContent = '还没有使用记录';
  empty.querySelector('span').textContent = '转写、识别图片或跑一次解析之后，这里会显示估算消耗。';

  const total = (data && data.total) || {};
  const count = (data && data.event_count) || 0;
  const cost = Number(total.estimated_cost) || 0;

  totalCost.textContent = fmtMoney(cost);
  totalSub.textContent = count ? `${count} 次调用` : '还没有使用记录';

  /* 单价回填：用户改过单价后，这里读到的就是 settings 里的值 */
  const p = (data && data.pricing) || {};
  if (p.asr_per_hour != null) $('#pAsr').value = p.asr_per_hour;
  if (p.ocr_per_image != null) $('#pOcr').value = p.ocr_per_image;
  if (p.llm_input_per_mtok != null) $('#pIn').value = p.llm_input_per_mtok;
  if (p.llm_output_per_mtok != null) $('#pOut').value = p.llm_output_per_mtok;

  if (!count) {
    empty.hidden = false;
    sections.hidden = true;
    return;
  }
  empty.hidden = true;
  sections.hidden = false;

  /* 按类型：只显示有量的种类，全零的行对用户没信息 */
  const byKind = (data.by_kind || []).filter((b) => {
    const tok = (Number(b.prompt_tokens) || 0) + (Number(b.completion_tokens) || 0);
    return (Number(b.audio_seconds) || 0) > 0 || (Number(b.images) || 0) > 0 || tok > 0
      || (Number(b.estimated_cost) || 0) > 0;
  });
  $('#uKinds').innerHTML = byKind.length
    ? byKind.map((b) => {
      const label = KIND_LABEL[b.key] || b.key || '其他';
      return `<div class="u-row">
        <span class="u-row-k">${esc(label)}</span>
        <span class="u-row-v">${esc(usageAmount(b.key, b))}</span>
        <span class="u-row-c">¥${fmtMoney(b.estimated_cost)}</span>
      </div>`;
    }).join('')
    : '<div class="u-row"><span class="u-row-v">还没有分类数据</span></div>';

  /* 按天：契约里 by_day 是倒序，取前 7 天；条的宽度按最大值归一 */
  const days = (data.by_day || []).slice(0, USAGE_BARS);
  const max = days.reduce((m, d) => Math.max(m, Number(d.estimated_cost) || 0), 0);
  /* 标题上写实际覆盖的日期区间，而不是「最近 N 天」。
     后端只返回有记录的日子时，N 天会误导 —— 7 根条可能横跨三周。 */
  const label = (() => {
    if (days.length < 2) return ''; // 只有一天时下面那行的日期就是它，别重复
    const short = (k) => String(k || '').slice(5);
    return `${short(days[days.length - 1].key)} – ${short(days[0].key)}`;
  })();
  $('#uDaysLabel').textContent = label;
  $('#uBars').innerHTML = days.length
    ? days.map((d) => {
      const c = Number(d.estimated_cost) || 0;
      const pct = max > 0 ? Math.max(c > 0 ? 2 : 0, Math.round((c / max) * 100)) : 0;
      /* 0 的那天不画填充块：留 2px 的 min-width 会在窄条上显出一个
         莫名其妙的小方块，看起来像「花了 2px 的钱」 */
      const fill = c > 0
        ? `<i class="u-bar-fill" style="width:${pct}%"></i>`
        : '';
      return `<div class="u-bar${c > 0 ? '' : ' zero'}" title="${esc(d.key)} · ¥${fmtMoney(c)}">
        <span class="u-bar-d">${esc(String(d.key || '').slice(5))}</span>
        <span class="u-bar-track">${fill}</span>
        <span class="u-bar-c">¥${fmtMoney(c)}</span>
      </div>`;
    }).join('')
    : '<div class="u-row"><span class="u-row-v">最近没有记录</span></div>';
}

async function savePricing(e) {
  if (e) e.preventDefault();
  showFormError('#uPricingError', '');
  const body = {
    asr_per_hour: Number($('#pAsr').value) || 0,
    ocr_per_image: Number($('#pOcr').value) || 0,
    llm_input_per_mtok: Number($('#pIn').value) || 0,
    llm_output_per_mtok: Number($('#pOut').value) || 0,
  };
  const btn = $('#uPricingSave');
  btn.disabled = true;
  try {
    await api('/settings/pricing', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    toast('单价已更新');
    /* 费用是后端按单价算的，改完必须重新拉一次，不能在前端自己乘一遍 */
    await loadUsage();
  } catch (err) {
    showFormError('#uPricingError', err.message);
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------ 事件绑定 */

function bind() {
  // ---- 设置面板（模型 / API Key / 用量）
  $('#settingsBtn').addEventListener('click', () => openSettings());
  $('#settingsClose').addEventListener('click', closeSettings);
  $('#settingsWrap').addEventListener('click', (e) => {
    /* 只有点在遮罩本身才关；点面板内部不能关 */
    if (e.target === $('#settingsWrap')) closeSettings();
  });
  $$('.stab').forEach((b) => {
    b.addEventListener('click', () => setSettingsTab(b.dataset.stab));
    /* tablist 的 roving tabindex：左右键切换，和结果面板那几个 tab 一致 */
    b.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      e.preventDefault();
      const next = b.dataset.stab === 'model' ? 'usage' : 'model';
      setSettingsTab(next);
      $(next === 'model' ? '#stabModel' : '#stabUsage').focus();
    });
  });

  // 显示 / 隐藏 Key
  $('#sApiKeyEye').addEventListener('click', () => toggleKeyVisible('#sApiKey', '#sApiKeyEye'));
  $('#sDsKeyEye').addEventListener('click', () => toggleKeyVisible('#sDsKey', '#sDsKeyEye'));

  // 保存 / 清除
  $('#sLlmSave').addEventListener('click', saveLLM);
  $('#sLlmClear').addEventListener('click', () => armClear('llm'));
  $('#sDsSave').addEventListener('click', saveDeepseek);
  $('#sDsClear').addEventListener('click', () => armClear('ds'));
  $('#sTestBtn').addEventListener('click', testConnection);
  $('#sModelFetch').addEventListener('click', fetchModels);

  /* 模型列表是渲染出来的，用事件委托，避免重渲染后失效 */
  $('#sModels').addEventListener('click', (e) => {
    const chip = e.target.closest('[data-model]');
    if (!chip) return;
    if (chip.dataset.nonchat === '1') {
      toast('这个模型不能用于对话（它是向量/重排类模型）', 'err');
      return;
    }
    $('#sModel').value = chip.dataset.model;
    $$('#sModels .s-model-chip').forEach((c) => c.classList.toggle('on', c === chip));
  });

  // 表单里按 Enter 等于点保存（只有单行输入，没有需要换行的东西）
  ['#sBaseUrl', '#sApiKey', '#sModel'].forEach((sel) => {
    $(sel).addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); saveLLM(); }
    });
  });
  $('#sDsKey').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); saveDeepseek(); }
  });

  // 用量
  $('#uRefresh').addEventListener('click', loadUsage);
  $('#uPricingForm').addEventListener('submit', savePricing);

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

  // ---- 解析
  // 面板里的按钮是渲染时生成的，用事件委托，避免每次重渲染都要重新绑
  const anaBody = $('#anaBody');
  anaBody.addEventListener('click', async (e) => {
    const startBtn = e.target.closest('[data-ana-start]');
    if (startBtn) {
      // 兜底：不可用时不该发出必然 400 的请求（正常路径下这个按钮不会渲染出来）
      if (!state.anaAvailable) {
        toast(analysisReason(), 'err', 5200);
        return;
      }
      startBtn.disabled = true;
      startAnalyze(false);
      return;
    }

    /* 时间戳：核心交互，跳到分句并高亮 */
    const jumpBtn = e.target.closest('[data-jump]');
    if (jumpBtn) {
      revealSegmentAt(jumpBtn.dataset.jump);
      return;
    }

    /* 金句复制 */
    const copyBtn = e.target.closest('[data-copy-quote]');
    if (copyBtn) {
      const q = (state.analysis && state.analysis.quotes) || [];
      const item = q[Number(copyBtn.dataset.copyQuote)];
      if (!item) return;
      const ok = await copy(item.text || '');
      if (ok) {
        copyBtn.classList.add('done');
        setTimeout(() => copyBtn.classList.remove('done'), 1600);
      }
      toast(ok ? '已复制这句原话' : '复制失败', ok ? '' : 'err');
    }
  });

  $('#anaRedo').addEventListener('click', () => armReanalyze($('#anaRedo')));
  $('#anaCopyAll').addEventListener('click', async () => {
    const md = analysisMarkdown();
    if (!md) { toast('还没有解析结果'); return; }
    const ok = await copy(md);
    toast(ok ? '已复制解析结果' : '复制失败', ok ? '' : 'err');
  });

  // tab 键盘导航：左右箭头在 tab 之间移动（ARIA tablist 的标准行为）
  $('.tabs').addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const tabs = $$('.tab').filter((t) => !t.hidden);
    const cur = tabs.indexOf(document.activeElement);
    if (cur < 0) return;
    e.preventDefault();
    const next = tabs[(cur + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    next.focus();
    setTab(next.dataset.tab);
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

    /* 设置面板是模态的：开着的时候其他全局快捷键（⌘K、⌘Enter、/）都不该生效，
       只留 Esc 关闭 + Tab 陷阱。否则用户在里面按 ⌘K 会叠出第二层弹层。 */
    if (!$('#settingsWrap').hidden) {
      if (e.key === 'Escape') { e.preventDefault(); closeSettings(); return; }
      trapSettingsFocus(e);
      return;
    }

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
  // tablist 的 roving tabindex 初值：只有「正文」在 Tab 序列里
  $$('.tab').forEach((t) => { t.tabIndex = t.classList.contains('on') ? 0 : -1; });
  autosize($('#urlInput'));
  $('#urlInput').focus();
  loadCapabilities();
  loadHistory();
}

init();
