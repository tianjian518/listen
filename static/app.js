/* Listen · 前端逻辑（侧边栏三页 + 播放器 + 302 播放） */
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const HOT = ['周杰伦', '邓紫棋', '陈奕迅', '五月天', '林俊杰', '毛不易', '李荣浩', '张学友'];

let status = { logged_in: false, yt_cookie: false };
let qrTimer = null;
let taskTimer = null;

let player = { list: [], idx: -1, retried: false, now: false };
const audio = () => $('#audio');

/* ---------------- 工具 ---------------- */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
function toast(msg, isErr) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast' + (isErr ? ' err' : '');
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 2600);
}
function fmtSize(bytes) {
  if (!bytes) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, n = bytes;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + ' ' + u[i];
}
async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  let data = null;
  try { data = await r.json(); } catch (e) { data = {}; }
  if (!r.ok) {
    const msg = (data && (data.detail || data.message)) || ('请求失败 ' + r.status);
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    err.status = r.status;
    throw err;
  }
  return data;
}

/* ---------------- 页面路由 ---------------- */
function go(page) {
  $$('.page').forEach((p) => { p.hidden = p.id !== 'page-' + page; });
  $$('.nav-item').forEach((n) => n.classList.toggle('active', n.dataset.page === page));
  if (page === 'player') loadLibrary();
  if (page === 'tasks') loadTasks();
  if (page === 'settings') { loadStatus(); loadSourceList(); renderSettings(); renderInvHosts(); }
  refreshPlayerUI();
  window.scrollTo(0, 0);
}

/* ---------------- 云盘状态 ---------------- */
async function loadStatus() {
  try {
    status = await api('/api/status');
  } catch (e) {
    status = { logged_in: false, yt_cookie: false, reason: e.message };
  }
  renderCloudChip();
  renderSettings();
}

function renderCloudChip() {
  const dot = $('#cloudDot'), txt = $('#cloudText');
  if (status.logged_in) {
    dot.className = 'dot ok';
    txt.textContent = '天翼云盘 · 剩余 ' + fmtSize(status.free);
  } else {
    dot.className = 'dot off';
    txt.textContent = '连接天翼云盘';
  }
  const sd = $('#srcDot'), st = $('#srcText');
  sd.className = 'dot ok';
  st.textContent = status.yt_cookie ? '音源 · 加速已开启' : '音源已就绪';
}

function renderSettings() {
  const cb = $('#setCloudBody');
  if (cb) {
    cb.innerHTML = status.logged_in
      ? `<div class="set-row">
           <div><b>${esc(status.nickname || status.account || '已连接')}</b>
           <span class="muted">总 ${fmtSize(status.total)} · 已用 ${fmtSize(status.used)}</span></div>
           <button class="btn ghost sm" onclick="openCloud()">管理</button>
         </div>`
      : `<div class="set-row">
           <div><b>未连接</b><span class="muted">扫码即可，密码不经过本程序</span></div>
           <button class="btn sm" onclick="openCloud()">扫码连接</button>
         </div>`;
  }
  const fb = $('#setFolderBody');
  if (fb) {
    fb.innerHTML = status.logged_in
      ? `<div class="set-row">
           <div><b>「${esc(status.folder_name || '我的音乐')}」</b>
           <span class="muted">转存的歌都放这里</span></div>
           <button class="btn ghost sm" onclick="openFolder()">更改</button>
         </div>`
      : `<div class="muted">连接云盘后可设置</div>`;
  }
}

/* ---------------- 搜索 ---------------- */
function renderHot() {
  $('#hotChips').innerHTML = HOT.map((k) =>
    `<span class="chip" onclick="quickSearch('${esc(k)}')">${esc(k)}</span>`).join('');
}
function quickSearch(k) { go('search'); $('#q').value = k; doSearch(); }

async function doSearch() {
  const q = $('#q').value.trim();
  if (!q) { toast('先输入歌名或歌手', true); return; }
  const btn = $('#searchBtn');
  btn.disabled = true; btn.textContent = '搜索中';
  $('#results').innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="spinner" style="margin:0 auto 14px"></div>正在找…</div>`;
  try {
    const data = await api('/api/search', { method: 'POST', body: JSON.stringify({ q, limit: 18 }) });
    LS_LAST_Q = q;
    LS_LAST_ITEMS = data.items || [];
    renderResults(LS_LAST_ITEMS, q, false);
    saveSearchCache();
  } catch (e) {
    $('#results').innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="big"><svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"></circle><path d="M8 15s1.5-2 4-2 4 2 4 2M9 9h.01M15 9h.01"></path></svg></div>${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = '搜索';
  }
}

/* ---------------- 上次搜索结果缓存 ---------------- */
const LS_KEY = 'listen.lastSearch.v1';
let LS_LAST_Q = '';
let LS_LAST_ITEMS = [];

function saveSearchCache() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({
      q: LS_LAST_Q, t: Date.now(), items: LS_LAST_ITEMS.slice(0, 18),
    }));
  } catch (e) { /* 隐私模式 / 超配额：忽略 */ }
}

function loadSearchCache() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return false;
    const d = JSON.parse(raw);
    if (!d || !Array.isArray(d.items) || !d.items.length) return false;
    LS_LAST_Q = d.q || '';
    LS_LAST_ITEMS = d.items.slice(0, 18);
    if (LS_LAST_Q) $('#q').value = LS_LAST_Q;
    renderResults(LS_LAST_ITEMS, LS_LAST_Q, true);
    return true;
  } catch (e) {
    return false;
  }
}

function renderResults(items, keyword, stale) {
  const box = $('#results');
  if (!items.length) {
    box.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="big"><svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7"></circle><path d="M20 20l-3.5-3.5"></path></svg></div>没找到，换个关键词试试</div>`;
    return;
  }
  const head = keyword
    ? `<div class="results-head">
         <span class="rh-q">${stale ? '上次结果 · ' : ''}「${esc(keyword)}」</span>
         <span class="rh-n">${items.length} 首</span>
       </div>`
    : '';
  box.innerHTML = head + items.map((it, i) => `
    <article class="card" style="animation-delay:${Math.min(i * 22, 260)}ms">
      <div class="thumb">
        <img loading="lazy" src="${esc(it.thumb || '/api/thumb/' + it.id)}" alt=""
             onerror="this.onerror=()=>{this.style.opacity=.15};this.src='/api/thumb/${esc(it.id)}'">
        ${it.duration_text ? `<span class="dur">${esc(it.duration_text)}</span>` : ''}
      </div>
      <div class="card-body">
        <h3 class="card-title" title="${esc(it.title)}">${esc(it.title)}</h3>
        <div class="card-meta">${esc(it.uploader || '未知来源')}</div>
        <button class="save-btn" data-id="${esc(it.id)}" data-title="${esc(it.title)}"
                data-source="${esc(it.source || 'invidious')}" data-host="${esc(it.host || '')}"
                data-gid="${esc(it.gid || '')}" data-cover="${esc(it.thumb || it.cover || '')}"
                onclick="transfer(this)">转存到云盘</button>
      </div>
    </article>`).join('');
}

/* ---------------- 转存 ---------------- */
async function transfer(btn) {
  if (!status.logged_in) {
    openCloud();
    toast('先扫码连接天翼云盘', true);
    return;
  }
  const id = btn.dataset.id, title = btn.dataset.title;
  btn.disabled = true; btn.textContent = '提交中…';
  try {
    await api('/api/transfer', {
      method: 'POST',
      body: JSON.stringify({
        video_id: id, title,
        source: btn.dataset.source, host: btn.dataset.host,
        gid: btn.dataset.gid || '', cover: btn.dataset.cover || '',
      })
    });
    btn.classList.add('done');
    btn.textContent = '已加入队列';
    startTasks();
    pollTasks();
  } catch (e) {
    btn.disabled = false; btn.textContent = '转存到云盘';
    if (e.status === 401) { openCloud(); }
    toast(e.message, true);
  }
}

/* ---------------- 转存记录（独立页面） ---------------- */
let lastTasks = [];   // 最近一次拉到的任务，新的在前
let lastDoneSeen = -1; // 上次看到的已完成数量，用于判断有没有新歌进云盘
let lastTaskSig = '';   // 任务列表指纹，没变化就不重绘

function startTasks() {
  clearTimeout(taskTimer);
  taskTimer = setTimeout(pollTasks, 500);
}

async function pollTasks() {
  clearTimeout(taskTimer);
  try {
    const data = await api('/api/tasks');
    const items = (data.items || []).slice().reverse();
    const sig = items.map((t) => [t.id, t.state, Math.round((t.progress || 0) * 100), t.message].join(':')).join('|');

    if (sig !== lastTaskSig) {
      lastTaskSig = sig;
      lastTasks = items;
      saveTasksCache(items);
      updateNavBadge();
      if (!$('#page-tasks').hidden) renderTaskPage();
    }

    // 有新歌转存成功 → 曲库标记过期；正在看播放器就立刻更新
    const doneNow = lastTasks.filter((t) => t.state === 'done').length;
    if (lastDoneSeen >= 0 && doneNow > lastDoneSeen) {
      libDirty = true;
      if (!$('#page-player').hidden) refreshLibrary({});
    }
    lastDoneSeen = doneNow;
    const active = (data.items || []).some((t) => t.state === 'running' || t.state === 'queued');
    taskTimer = setTimeout(pollTasks, active ? 1200 : 20000);
  } catch (e) {
    taskTimer = setTimeout(pollTasks, 8000);
  }
}

/* 导航角标：进行中显示数量；全部结束但有失败时显示红色数字 */
function updateNavBadge() {
  const badge = $('#navBadge');
  if (!badge) return;
  const running = lastTasks.filter((t) => t.state === 'running' || t.state === 'queued').length;
  const failed = lastTasks.filter((t) => t.state === 'failed').length;
  if (running) {
    badge.hidden = false;
    badge.className = 'nav-badge';
    badge.textContent = running;
  } else if (failed) {
    badge.hidden = false;
    badge.className = 'nav-badge err';
    badge.textContent = failed;
  } else {
    badge.hidden = true;
  }
}

const TASKS_KEY = 'listen.tasks.v1';

function loadTasksCache() {
  try {
    const raw = localStorage.getItem(TASKS_KEY);
    if (!raw) return false;
    const d = JSON.parse(raw);
    if (!d || !Array.isArray(d.items) || !d.items.length) return false;
    lastTasks = d.items;
    updateNavBadge();
    if (!$('#page-tasks').hidden) renderTaskPage();
    return true;
  } catch (e) { return false; }
}

function saveTasksCache(items) {
  try {
    localStorage.setItem(TASKS_KEY, JSON.stringify({ ts: Date.now(), items: items.slice(0, 60) }));
  } catch (e) { /* 忽略 */ }
}

async function loadTasks(manual) {
  // 先用现有数据/缓存画出来，切页回来不白屏
  if (!lastTasks.length) loadTasksCache();
  renderTaskPage();
  await pollTasks();          // 后台再对一次，有变化会自己重绘
  if (manual) toast('已刷新');
}

function ago(ts) {
  if (!ts) return '';
  const d = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (d < 60) return '刚刚';
  if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
  if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
  const days = Math.floor(d / 86400);
  if (days === 1) return '昨天';
  if (days < 30) return days + ' 天前';
  return new Date(ts * 1000).toLocaleDateString('zh-CN');
}

function renderTaskPage() {
  const sum = $('#taskSum');
  const list = $('#taskList');
  if (!sum || !list) return;

  const items = lastTasks;
  const running = items.filter((t) => t.state === 'running' || t.state === 'queued').length;
  const done = items.filter((t) => t.state === 'done').length;
  const failed = items.filter((t) => t.state === 'failed').length;

  sum.innerHTML = `
    <div class="task-stat">
      <div class="ts-cell"><b class="ts-blue">${running}</b><span>进行中</span></div>
      <div class="ts-cell"><b class="ts-green">${done}</b><span>已完成</span></div>
      <div class="ts-cell"><b class="ts-red">${failed}</b><span>失败</span></div>
    </div>
    <div class="task-actions">
      ${failed ? `<button class="btn sm" onclick="retryAllFailed()">一键重试失败 ${failed} 条</button>` : ''}
      ${(done + failed) ? `<button class="btn ghost sm" onclick="clearTasks()">清空已结束</button>` : ''}
    </div>`;

  if (!items.length) {
    list.innerHTML = `<div class="empty">
      <div class="big"><svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 6h13M8 12h13M8 18h13"></path><path d="M3 6h.01M3 12h.01M3 18h.01"></path></svg></div>
      还没有转存记录<br>去「搜索转存」找一首歌吧</div>`;
    return;
  }

  const STATE = { queued: '排队中', done: '已完成', failed: '失败' };
  list.innerHTML = items.map((t) => {
    const pct = Math.round((t.progress || 0) * 100);
    const stateText = STATE[t.state] || t.stage || '处理中';
    const canRetry = t.state === 'failed';
    const canDel = t.state === 'done' || t.state === 'failed';
    const showBar = t.state === 'running' || t.state === 'queued';
    const msg = (t.state === 'done' && t.size)
      ? `已保存到云盘 · ${fmtSize(t.size)}`
      : (t.message || (t.size ? fmtSize(t.size) : ''));
    return `
      <div class="trow ${t.state}">
        <div class="trow-top">
          <span class="trow-title" title="${esc(t.title)}">${esc(t.title)}</span>
          <span class="trow-state ${t.state}">${esc(stateText)}</span>
        </div>
        ${showBar ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ''}
        <div class="trow-foot">
          <span class="trow-msg ${t.state === 'failed' ? 'err' : ''}">${esc(msg)}</span>
          <span class="trow-time">${ago(t.created)}</span>
          <span class="trow-acts">
            ${canRetry ? `<button class="mini-btn" onclick="retryTask('${t.id}')">重试</button>` : ''}
            ${canDel ? `<button class="mini-btn del" onclick="delTask('${t.id}')">删除</button>` : ''}
          </span>
        </div>
      </div>`;
  }).join('');
}

async function retryTask(id) {
  try {
    await api('/api/tasks/' + encodeURIComponent(id) + '/retry', { method: 'POST' });
    toast('已重新提交');
    pollTasks();
  } catch (e) {
    toast(e.message || '重试失败', true);
  }
}

async function retryAllFailed() {
  const n = lastTasks.filter((t) => t.state === 'failed').length;
  if (!n) return;
  if (!confirm(`重试全部 ${n} 条失败任务？`)) return;
  try {
    const d = await api('/api/tasks/retry-failed', { method: 'POST' });
    toast(`已重新提交 ${d.count} 条`);
    pollTasks();
  } catch (e) {
    toast(e.message || '重试失败', true);
  }
}

async function delTask(id) {
  try {
    await api('/api/tasks/' + encodeURIComponent(id), { method: 'DELETE' });
    lastTasks = lastTasks.filter((t) => t.id !== id);
    renderTaskPage();
    updateNavBadge();
    toast('已删除该记录');
  } catch (e) {
    toast(e.message || '删除失败', true);
  }
}

async function clearTasks() {
  const n = lastTasks.filter((t) => t.state === 'done' || t.state === 'failed').length;
  if (!n) return;
  if (!confirm(`清空 ${n} 条已结束的记录？\n\n只清记录，云盘里的歌不受影响。`)) return;
  try {
    const d = await api('/api/tasks', { method: 'DELETE' });
    toast(`已清除 ${d.removed} 条`);
    pollTasks();
  } catch (e) {
    toast(e.message || '清除失败', true);
  }
}

/* ---------------- 播放器（云盘曲库） ---------------- */
const LIB_KEY = 'listen.library.v1';
const LIB_TTL = 10 * 60 * 1000;   // 缓存 10 分钟内直接用
let libDirty = false;             // 有新转存 / 删歌，需要重拉
let libBusy = false;

function libCacheGet() {
  try {
    const raw = localStorage.getItem(LIB_KEY);
    if (!raw) return null;
    const d = JSON.parse(raw);
    if (!d || !Array.isArray(d.songs)) return null;
    return d;
  } catch (e) { return null; }
}

function libCacheSet(songs, folder) {
  try {
    localStorage.setItem(LIB_KEY, JSON.stringify({ songs, folder: folder || '', ts: Date.now() }));
  } catch (e) { /* 隐私模式或超配额：忽略 */ }
}

/* 进入播放器页：有缓存就直接渲染，不重复请求云盘 */
async function loadLibrary(force) {
  if (force) return refreshLibrary({ fresh: true, notify: true });
  if (libDirty) return refreshLibrary({});
  const c = libCacheGet();
  if (c) {
    player.list = c.songs;
    $('#libFolder').textContent = c.folder ? `· ${c.folder}` : '';
    renderLibOrEmpty();
    if (Date.now() - (c.ts || 0) > LIB_TTL) refreshLibrary({ silent: true });
    return;
  }
  return refreshLibrary({ loading: true });
}

/* 真正拉云盘曲库：loading 显示骨架，fresh 绕过服务端缓存，silent 失败不打扰 */
async function refreshLibrary(o) {
  const opt = o || {};
  if (libBusy) return;
  libBusy = true;
  const box = $('#libList');
  if (opt.loading && !player.list.length) {
    box.innerHTML = `<div class="empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  }
  try {
    const d = await api('/api/library' + (opt.fresh ? '?fresh=1' : ''));
    player.list = d.songs || [];
    $('#libFolder').textContent = d.folder_name ? `· ${d.folder_name}` : '';
    libCacheSet(player.list, d.folder_name);
    libDirty = false;
    renderLibOrEmpty();
  } catch (e) {
    if (!player.list.length) renderLibError(e);
    if (opt.notify) toast(e.message, true);
  } finally {
    libBusy = false;
  }
}

function renderLibOrEmpty() {
  if (!player.list.length) {
    $('#libList').innerHTML = `<div class="empty"><div class="big"><svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg></div>云盘里还没有歌<br>去「搜索转存」第一首吧</div>`;
    return;
  }
  renderLib();
}

function renderLibError(e) {
  $('#libList').innerHTML = e.status === 401
    ? `<div class="empty"><div class="big"><svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="10" width="16" height="11" rx="2"></rect><path d="M8 10V7a4 4 0 0 1 8 0v3"></path></svg></div>先在设置里连接天翼云盘<br><br><button class="btn" onclick="openCloud()">扫码连接</button></div>`
    : `<div class="empty"><div class="big"><svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"></circle><path d="M8 15s1.5-2 4-2 4 2 4 2M9 9h.01M15 9h.01"></path></svg></div>${esc(e.message)}</div>`;
}

function renderLib() {
  const NOTE = `<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:#7f8ea6"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>`;
  const EQ = `<span class="eq"><i></i><i></i><i></i></span>`;
  const box = $('#libList');
  box.innerHTML = player.list.map((s, i) => `
    <div class="lib-row ${i === player.idx ? 'playing' : ''}" onclick="playSong(${i})">
      <div class="lib-ico">${i === player.idx ? EQ : NOTE}</div>
      <div class="lib-name">
        <b>${esc(s.title)}</b>
        <span>${fmtSize(s.size)}</span>
      </div>
      <button class="icon-btn" title="播放" onclick="event.stopPropagation();playSong(${i})">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M8 5v14l11-7z"></path></svg>
      </button>
      <button class="icon-btn del" title="从云盘删除" onclick="event.stopPropagation();delSong(${i})">
        <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"></path><path d="M10 11v5M14 11v5"></path></svg>
      </button>
    </div>`).join('');
}

function delSong(i) {
  const s = player.list[i];
  if (!s) return;
  if (!confirm(`从云盘删除这首歌？\n\n${s.title}\n\n删除后进天翼云盘回收站，可以还原。`)) return;
  fetch('/api/library/' + encodeURIComponent(s.id), { method: 'DELETE' })
    .then((r) => r.json().catch(() => ({})).then((d) => {
      if (!r.ok) throw new Error(d.detail || d.message || ('删除失败 ' + r.status));
      const wasPlaying = player.idx === i;
      player.list.splice(i, 1);
      if (wasPlaying) {
        const a = audio();
        a.pause(); a.removeAttribute('src'); a.load();
        player.idx = -1; player.retried = false; player.now = false;
        refreshPlayerUI();
      } else if (player.idx > i) {
        player.idx--;
      }
      renderLib();
      toast('已删除：' + s.title);
      loadLibrary(true);
    }))
    .catch((e) => toast(e.message || '删除失败', true));
}

function playSong(i) {
  if (!player.list.length) return;
  player.idx = (i + player.list.length) % player.list.length;
  const song = player.list[player.idx];
  player.retried = false;
  player.now = true;
  const a = audio();
  a.src = `/api/play/${encodeURIComponent(song.id)}?t=${Date.now()}`;
  a.play().catch(() => {});
  $('#mpTitle').textContent = song.title;
  $('#mpArtist').textContent = '天翼云盘 · 直接播放';
  $('#nowTitle').textContent = song.title;
  $('#nowBar').style.width = '0%';
  $('#nowCur').textContent = '0:00';
  $('#nowDur').textContent = '0:00';
  setPlayIcon(true);
  refreshPlayerUI();
  renderLib();
  ensureDuration();
}

/* 网络慢时元数据可能迟迟不来：最多盯 12 秒，拿到就把时长填上 */
function ensureDuration() {
  const a = audio();
  let n = 0;
  const timer = setInterval(() => {
    n += 1;
    if (a.duration) {
      $('#nowDur').textContent = fmtTime(a.duration);
      clearInterval(timer);
    } else if (n > 24) {
      clearInterval(timer);
    }
  }, 500);
}

/* 播放器页显示「正在播放」大卡片；其他页显示底部悬浮条，两者不重复 */
function refreshPlayerUI() {
  const has = !!player.now && player.idx >= 0 && !!player.list[player.idx];
  const onPlayer = !$('#page-player').hidden;
  $('#nowCard').hidden = !(has && onPlayer);
  $('#miniPlayer').hidden = !(has && !onPlayer);
  document.body.classList.toggle('with-mini', !$('#miniPlayer').hidden);
  if (has) $('#mpTitle').textContent = player.list[player.idx].title;
}

function togglePlay() {
  const a = audio();
  if (!a.src) { if (player.list.length) playSong(player.idx < 0 ? 0 : player.idx); return; }
  if (a.paused) { a.play().catch(() => {}); } else { a.pause(); }
}
function setPlayIcon(playing) {
  document.body.classList.toggle('playing', !!playing);
  $('#mpIconPlay').style.display = playing ? 'none' : '';
  $('#mpIconPause').style.display = playing ? '' : 'none';
  const np = $('#nowIconPlay'), nq = $('#nowIconPause');
  if (np) np.style.display = playing ? 'none' : '';
  if (nq) nq.style.display = playing ? '' : 'none';
}

function fmtTime(sec) {
  if (!sec || !isFinite(sec)) return '0:00';
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return m + ':' + String(s).padStart(2, '0');
}
function prevSong() { if (player.list.length) playSong(player.idx - 1); }
function nextSong() { if (player.list.length) playSong(player.idx + 1); }

function seek(ev) {
  const a = audio();
  if (!a.duration) return;
  const rect = ev.currentTarget.getBoundingClientRect();
  a.currentTime = ((ev.clientX - rect.left) / rect.width) * a.duration;
}

function bindAudio() {
  const a = audio();
  // 元数据一到就显示总时长，不必等播放推进
  const syncDur = () => {
    $('#nowDur').textContent = fmtTime(a.duration);
    if (a.duration && a.currentTime) {
      const pct = (a.currentTime / a.duration * 100) + '%';
      $('#mpBar').style.width = pct;
      $('#nowBar').style.width = pct;
    }
  };
  a.addEventListener('loadedmetadata', syncDur);
  a.addEventListener('durationchange', syncDur);
  a.addEventListener('timeupdate', () => {
    if (a.duration) {
      const pct = (a.currentTime / a.duration * 100) + '%';
      $('#mpBar').style.width = pct;
      $('#nowBar').style.width = pct;
    }
    $('#nowCur').textContent = fmtTime(a.currentTime);
    $('#nowDur').textContent = fmtTime(a.duration);
  });
  a.addEventListener('play', () => setPlayIcon(true));
  a.addEventListener('pause', () => setPlayIcon(false));
  a.addEventListener('ended', () => nextSong());
  a.addEventListener('error', () => {
    const song = player.list[player.idx];
    if (!song) return;
    if (!player.retried) {
      // 直链可能过期：重新取一次 302
      player.retried = true;
      a.src = `/api/play/${encodeURIComponent(song.id)}?t=${Date.now()}`;
      a.play().catch(() => {});
    } else {
      toast('播放失败，云盘可能已删除该文件', true);
      setPlayIcon(false);
    }
  });
}

/* ---------------- 扫码 ---------------- */
function openCloud() {
  $('#qrModal').hidden = false;
  if (status.logged_in) {
    $('#qrTitle').textContent = '天翼云盘已连接';
    $('#qrTip').textContent = `当前账号：${status.account || '-'} · 保存到「${status.folder_name || '我的音乐'}」`;
    $('#qrBox').hidden = true;
    $('#qrState').textContent = '剩余空间 ' + fmtSize(status.free);
    $('#qrState').className = 'qr-state ok';
    $('#qrRefresh').textContent = '重新扫码';
    $('#qrLogout').hidden = false;
  } else {
    $('#qrTitle').textContent = '连接天翼云盘';
    $('#qrTip').textContent = '打开手机「天翼云盘」App，扫下面的二维码';
    $('#qrBox').hidden = false;
    $('#qrLogout').hidden = true;
    $('#qrRefresh').textContent = '换个二维码';
    startQr();
  }
}
function closeQr() { $('#qrModal').hidden = true; clearInterval(qrTimer); qrTimer = null; }

async function startQr() {
  $('#qrImg').hidden = true;
  $('#qrPlaceholder').hidden = false;
  $('#qrState').textContent = '正在生成二维码…';
  $('#qrState').className = 'qr-state';
  try { await api('/api/qr/start', { method: 'POST' }); }
  catch (e) {
    $('#qrState').textContent = e.message;
    $('#qrState').className = 'qr-state err';
    return;
  }
  clearInterval(qrTimer);
  qrTimer = setInterval(pollQr, 1400);
  pollQr();
}

async function pollQr() {
  let s;
  try { s = await api('/api/qr/state'); } catch (e) { return; }
  if (s.qr && s.state !== 'success') {
    const img = $('#qrImg');
    if (img.src !== s.qr) img.src = s.qr;
    img.hidden = false;
    $('#qrPlaceholder').hidden = true;
  }
  const st = $('#qrState');
  if (s.state === 'success') {
    st.textContent = '登录成功，正在读取账号…';
    st.className = 'qr-state ok';
    clearInterval(qrTimer);
    setTimeout(async () => { await loadStatus(); closeQr(); loadLibrary(true); toast('云盘已连接'); }, 600);
    return;
  }
  if (s.state === 'waiting') { st.textContent = '等待扫码…'; st.className = 'qr-state'; }
  else if (s.state === 'scanned') { st.textContent = '已扫描，请在手机上点「确认」'; st.className = 'qr-state ok'; }
  else if (s.state === 'error') { st.textContent = s.message || '出错了'; st.className = 'qr-state err'; clearInterval(qrTimer); }
  else if (s.state === 'starting') { st.textContent = '正在启动浏览器…'; }
}

function refreshQr() { status.logged_in ? openCloud() : startQr(); }

async function logout() {
  await api('/api/logout', { method: 'POST' });
  status = { logged_in: false, yt_cookie: status.yt_cookie };
  renderCloudChip(); closeQr(); toast('已断开云盘连接');
}

/* ---------------- 音源状态 ---------------- */
function openSrc() { go('settings'); setTimeout(loadSourceList, 100); }

async function loadSourceList() {
  const box = $('#srcList');
  try {
    const d = await api('/api/sources');
    if (!d.items || !d.items.length) {
      box.innerHTML = `<div class="src-state">首次探测进行中，几秒后自动刷新…</div>`;
      setTimeout(loadSourceList, 3000);
      return;
    }
    box.innerHTML = d.items.map((s) => `
      <div class="src-row">
        <span class="dot ${s.ok ? 'ok' : 'off'}"></span>
        <div class="src-info">
          <b>${esc(s.label)}</b>
          <span>${esc(s.note || '')}</span>
        </div>
        <span class="src-lat">${s.ok ? (s.latency + 's') : '不可用'}</span>
      </div>`).join('');
  } catch (e) {
    box.innerHTML = `<div class="src-state err">${esc(e.message)}</div>`;
  }
}

/* ---------------- 自定义公益镜像 ---------------- */
async function renderInvHosts() {
  const box = $('#invHostList');
  if (!box) return;
  try {
    const d = await api('/api/invhosts');
    box.innerHTML = (d.hosts || []).length
      ? d.hosts.map((h) => `
          <div class="src-row">
            <span class="dot ok"></span>
            <div class="src-info"><b>${esc(h)}</b><span>自定义实例</span></div>
            <button class="btn ghost sm" onclick="removeInvHost('${esc(h)}')">删除</button>
          </div>`).join('')
      : `<div class="muted" style="font-size:12.5px">还没有添加自定义实例</div>`;
  } catch (e) { /* 静默 */ }
}

async function addInvHost() {
  const inp = $('#invHostInput');
  const host = (inp.value || '').trim();
  if (!host) { toast('先填域名', true); return; }
  try {
    await api('/api/invhosts', { method: 'POST', body: JSON.stringify({ host }) });
    inp.value = '';
    toast('已添加，正在体检…');
    renderInvHosts();
    setTimeout(() => { loadSourceList(); renderInvHosts(); }, 8000);
  } catch (e) {
    toast(e.message, true);
  }
}

async function removeInvHost(host) {
  await api('/api/invhosts/' + encodeURIComponent(host), { method: 'DELETE' });
  renderInvHosts();
  setTimeout(loadSourceList, 500);
}

async function uploadCookie(input) {
  const f = input.files && input.files[0];
  if (!f) return;
  const st = $('#srcState');
  st.className = 'src-state'; st.textContent = '上传中…';
  const fd = new FormData();
  fd.append('file', f);
  try {
    const r = await fetch('/api/ytcookie', { method: 'POST', body: fd });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || '上传失败');
    st.textContent = '上传成功';
    await loadStatus();
    loadSourceList();
    toast('YouTube 加速已开启');
  } catch (e) {
    st.className = 'src-state err'; st.textContent = e.message;
  }
  input.value = '';
}

async function clearCookie() {
  await fetch('/api/ytcookie', { method: 'DELETE' });
  await loadStatus(); toast('已清除凭证');
}

/* ---------------- 保存位置 ---------------- */
let folderStack = [{ id: '-11', name: '根目录' }];
let folderSel = null;

async function openFolder() {
  if (!status.logged_in) { openCloud(); return; }
  folderStack = [{ id: '-11', name: '根目录' }];
  folderSel = null;
  await loadFolders('-11');
  // 复用扫码弹窗结构会太绕，直接用 toast 引导 + 内联修改
  const name = prompt('输入要保存到的文件夹名（在云盘「我的音乐」下，留空则用当前设置）', status.folder_name || '我的音乐');
  if (name === null) return;
  const trimmed = name.trim() || '我的音乐';
  await api('/api/folder', { method: 'POST', body: JSON.stringify({ name: trimmed, auto_create: true, id: '-11' }) });
  await loadStatus();
  toast('保存位置已更新为「' + trimmed + '」');
}

async function loadFolders(id) { /* 目录浏览器已简化为按名自动创建 */ }
function renderCrumb() {}
function jumpCrumb(i) {}
function selectHere(id) {}
async function chooseCurrent() {}

/* ---------------- 启动 ---------------- */
$('#q') && $('#q').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });

(async function init() {
  renderHot();
  bindAudio();
  // 先把本地缓存吐出来，页面一打开就有内容，不用等网络
  loadSearchCache();
  loadTasksCache();
  loadStatus();     // 不 await，避免挡住上面的渲染
  pollTasks();
})();
