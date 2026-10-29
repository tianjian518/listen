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
  $$('.nav-item').forEach((n) => n.classList.toggle(
    'active', n.dataset.page === page || (page === 'book' && n.dataset.page === 'books')));
  if (page === 'player') { loadLibrary(); scheduleLibSync(); }
  else { clearTimeout(libTimer); }
  if (page === 'books') loadBooks();
  if (page === 'tasks') loadTasks();
  if (page === 'convert') cvEnter(); else clearInterval(cv.timer);
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
  renderBookFolder();
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

/* ---------------- 重命名弹窗（转存前 / 云盘已有 共用） ---------------- */
let rnCtx = null;

function openRename(opts) {
  rnCtx = opts;
  $('#rnHeading').textContent = opts.heading || '重命名';
  $('#rnTip').textContent = opts.tip || '';
  $('#rnInput').value = opts.value || '';
  $('#rnOkBtn').textContent = opts.okText || '确定';
  $('#rnModal').hidden = false;
  setTimeout(() => {
    const i = $('#rnInput');
    i.focus();
    i.select();
  }, 60);
}

function closeRename() {
  $('#rnModal').hidden = true;
  rnCtx = null;
}

async function submitRename() {
  if (!rnCtx) return;
  const v = ($('#rnInput').value || '').trim();
  if (!v) { toast('名字不能为空', true); return; }
  const btn = $('#rnOkBtn');
  btn.disabled = true;
  try {
    await rnCtx.onOk(v);
    closeRename();
  } catch (e) {
    toast(e.message || '改名失败', true);
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- 转存 ---------------- */
function transfer(btn) {
  if (!status.logged_in) {
    openCloud();
    toast('先扫码连接天翼云盘', true);
    return;
  }
  const title = btn.dataset.title || '';
  openRename({
    heading: '转存前改个名',
    tip: '存进云盘后就用这个名字。不想改的话，直接点「开始转存」。',
    value: title,
    okText: '开始转存',
    onOk: (newTitle) => doTransfer(btn, newTitle),
  });
}

async function doTransfer(btn, title) {
  const id = btn.dataset.id;
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
let libDirty = false;             // 有新转存 / 删歌，需要重拉
let libBusy = false;
let libSig = '';                  // 曲库指纹：内容没变就不重绘
let libTimer = null;              // 停在播放器页时，定时对一次云盘

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
    libSig = c.songs.map((s) => `${s.id}:${s.title}:${s.size}`).join('|');
    // 缓存先顶上，同时静默对一次云盘：别的设备转存的歌，进来就能同步到
    refreshLibrary({ silent: true });
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
    const songs = d.songs || [];
    const sig = songs.map((s) => `${s.id}:${s.title}:${s.size}`).join('|');
    $('#libFolder').textContent = d.folder_name ? `· ${d.folder_name}` : '';
    libCacheSet(songs, d.folder_name);
    libDirty = false;

    if (sig !== libSig) {
      // 记住当前在放哪首，换完列表按 id 找回来，避免歌曲错位
      const cur = player.list[player.idx];
      player.list = songs;
      libSig = sig;
      if (cur) {
        const ni = songs.findIndex((s) => s.id === cur.id);
        player.idx = ni;
        if (ni < 0) {   // 这首在云盘里被删了
          const a = audio();
          a.pause(); a.removeAttribute('src'); a.load();
          player.now = false;
          refreshPlayerUI();
        }
      }
      renderLibOrEmpty();
    }
  } catch (e) {
    if (!player.list.length) renderLibError(e);
    if (opt.notify) toast(e.message, true);
  } finally {
    libBusy = false;
  }
}

/* 停在播放器页不动时，每分钟静默对一次云盘，别处传的歌也能自己冒出来 */
function scheduleLibSync() {
  clearTimeout(libTimer);
  if ($('#page-player').hidden) return;
  libTimer = setTimeout(async () => {
    await refreshLibrary({ silent: true });
    scheduleLibSync();
  }, 60000);
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
      <button class="icon-btn" title="重命名" onclick="event.stopPropagation();renameSong(${i})">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"></path><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"></path></svg>
      </button>
      <button class="icon-btn del" title="从云盘删除" onclick="event.stopPropagation();delSong(${i})">
        <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"></path><path d="M10 11v5M14 11v5"></path></svg>
      </button>
    </div>`).join('');
}

/* 给云盘里已有的歌改名（走天翼改名接口，手机 App 里同步生效） */
function renameSong(i) {
  const s = player.list[i];
  if (!s) return;
  openRename({
    heading: '重命名',
    tip: '会同时改掉云盘里的文件名，在天翼 App 里看到的也是新名字。',
    value: s.title,
    okText: '保存',
    onOk: async (v) => {
      const d = await api('/api/library/' + encodeURIComponent(s.id) + '/rename', {
        method: 'POST',
        body: JSON.stringify({ title: v, current_name: s.name || '' }),
      });
      s.title = d.title;
      s.name = d.name;
      const c = libCacheGet();
      libCacheSet(player.list, (c && c.folder) || '');
      renderLib();
      if (player.idx === i) {
        $('#nowTitle').textContent = d.title;
        $('#mpTitle').textContent = d.title;
      }
      toast('已改名为「' + d.title + '」');
    },
  });
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
  mode = 'music';                 // 音乐与听书共用同一个播放器，互斥切换
  resetBookTimer();
  const a = audio();
  a.playbackRate = 1;
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
  const isBookMode = mode === 'book' && book.idx >= 0 && !!book.list[book.idx];
  const hasMusic = mode === 'music' && !!player.now && player.idx >= 0 && !!player.list[player.idx];
  const onPlayer = !$('#page-player').hidden;

  $('#nowCard').hidden = !(hasMusic && onPlayer);

  const showMini = isBookMode ? true : (hasMusic && !onPlayer);
  $('#miniPlayer').hidden = !showMini;
  document.body.classList.toggle('with-mini', showMini);

  if (isBookMode) {
    const ch = book.list[book.idx];
    $('#mpTitle').textContent = ch.name;
    $('#mpArtist').textContent = book.name + ' · ' + speedText();
  } else if (hasMusic) {
    $('#mpTitle').textContent = player.list[player.idx].title;
    $('#mpArtist').textContent = '天翼云盘 · 直接播放';
  }

  const info = $('.mp-info');
  if (info) {
    info.onclick = () => {
      if (mode === 'book' && book.idx >= 0) toggleBookPanel(true);
      else go('player');
    };
  }
}

function togglePlay() {
  if (mode === 'book') { bookToggle(); return; }
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
function prevSong() {
  if (mode === 'book') { bookPrev(); return; }
  if (player.list.length) playSong(player.idx - 1);
}

function nextSong() {
  if (mode === 'book') { bookNext(); return; }
  if (player.list.length) playSong(player.idx + 1);
}

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
    if (mode === 'book') { bookOnMeta(); return; }
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
    if (mode === 'book') { bookTick(); return; }
    if (a.duration) {
      const pct = (a.currentTime / a.duration * 100) + '%';
      $('#mpBar').style.width = pct;
      $('#nowBar').style.width = pct;
    }
    $('#nowCur').textContent = fmtTime(a.currentTime);
    $('#nowDur').textContent = fmtTime(a.duration);
  });
  a.addEventListener('play', () => { setPlayIcon(true); bookSetIcon(true); });
  a.addEventListener('pause', () => {
    setPlayIcon(false);
    bookSetIcon(false);
    if (mode === 'book') saveProgress();
  });
  a.addEventListener('ended', () => {
    if (mode === 'book') { bookOnEnded(); return; }
    nextSong();
  });
  a.addEventListener('error', () => {
    if (mode === 'book') { bookOnError(); return; }
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
  else if (s.state === 'starting') { st.textContent = '正在获取二维码…'; }
  else if (s.state === 'expired') { st.textContent = s.message || '二维码过期，点重新生成'; st.className = 'qr-state err'; clearInterval(qrTimer); }
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

/* ============================ 听书 ============================ */
const SHELF_KEY = 'listen.shelf.v1';
const SPEED_KEY = 'listen.speed.v1';
const SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2];
const TIMER_MIN = [0, 15, 30, 45, 60];
const CH_PAGE = 80;
const CH_SEG = 50;    // 没分卷的书，每 50 集折一段

const ICON_FOLDER = '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path></svg>';
const ICON_HEADPHONE = '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 14v-2a9 9 0 0 1 18 0v2"></path><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"></path></svg>';
const ICON_WARN = '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"></circle><path d="M8 15s1.5-2 4-2 4 2 4 2M9 9h.01M15 9h.01"></path></svg>';

// music / book 共用一个 <audio>，天然互斥
let mode = 'music';
let shelf = { root: '', exists: false, list: [], ts: 0 };
let book = {
  id: '', name: '', single: false,
  raw: [], list: [], orderMap: {},
  idx: -1, heard: -1, progress: {},
  sortBy: 'name', shown: CH_PAGE, pendingSeek: 0,
  openVols: {}, volInit: false, volFolded: false,
};
let bookSpeed = 1;
let bookLoopOn = false;
let timerIdx = 0;
let timerHandle = null;
let lastSaveAt = 0;
let scrollStamp = 0;
let nameOrder = '';
let nameOrderLoaded = false;

try {
  const v = parseFloat(localStorage.getItem(SPEED_KEY) || '1');
  if (SPEEDS.indexOf(v) >= 0) bookSpeed = v;
} catch (e) { /* 忽略 */ }

/* ---------- 小工具 ---------- */
const BOOK_COLORS = [
  ['#4b6fd6', '#8a5cd8'], ['#2f8f8a', '#4fb3a8'], ['#b8643f', '#d89a5c'],
  ['#5c6f8f', '#93a4c0'], ['#8a5a9e', '#c07ac0'], ['#3f7fae', '#5fb0d8'],
  ['#7a6a3f', '#b9a35c'], ['#9e5a6a', '#d08a9a'],
];

function bookColor(name) {
  let h = 0;
  const s = name || '书';
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 9973;
  return BOOK_COLORS[h % BOOK_COLORS.length];
}

function speedText() {
  return String(bookSpeed).replace(/(\.\d)0$/, '$1') + 'x';
}

function cacheWrite(k, v) {
  try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* 忽略 */ }
}

function cacheRead(k) {
  try { return JSON.parse(localStorage.getItem(k) || 'null'); } catch (e) { return null; }
}

/* ---------- 书架 ---------- */
async function loadBooks(force, silent) {
  const box = $('#bookGrid');
  if (!shelf.list.length) {
    const c = cacheRead(SHELF_KEY);
    if (c && Array.isArray(c.list) && c.list.length) { shelf = c; renderShelf(); }
  }
  try {
    const d = await api('/api/books' + (force ? '?fresh=1' : ''));
    shelf = { root: d.root || '', exists: !!d.exists, list: d.books || [], ts: Date.now() };
    cacheWrite(SHELF_KEY, shelf);
    renderShelf();
    // 集数是后台数的，数完自己回来拿一次，别让用户盯着「统计中」
    clearTimeout(shelfCountTimer);
    if (d.counting || shelf.list.some((b) => !b.single && !b.count)) {
      shelfCountTries++;
      if (shelfCountTries <= 6) {
        shelfCountTimer = setTimeout(() => {
          if (!$('#page-books') || !$('#page-books').hidden) loadBooks(false, true);
        }, 2200);
      }
    } else {
      shelfCountTries = 0;
    }
  } catch (e) {
    if (!shelf.list.length) {
      box.innerHTML = '<div class="empty" style="grid-column:1/-1"><div class="big">' + ICON_WARN + '</div>' + esc(e.message) + '</div>';
    }
    if (force) toast(e.message, true);
  }
}
let shelfCountTimer = 0, shelfCountTries = 0;

function renderShelf() {
  const box = $('#bookGrid');
  $('#bookRoot').textContent = shelf.root ? '· ' + shelf.root : '';

  if (!shelf.exists) {
    box.innerHTML = '<div class="empty" style="grid-column:1/-1">'
      + '<div class="big">' + ICON_FOLDER + '</div>'
      + '云盘里还没有「' + esc(shelf.root || '有声书') + '」这个文件夹<br>'
      + '<span style="font-size:12px;color:#9aa8bd">在云盘根目录建一个同名文件夹，<br>一本有声书放一个子文件夹，再点刷新</span>'
      + '<br><button class="btn ghost sm" onclick="editBookFolder()">改听书目录</button>'
      + '</div>';
    return;
  }
  if (!shelf.list.length) {
    box.innerHTML = '<div class="empty" style="grid-column:1/-1">'
      + '<div class="big">' + ICON_HEADPHONE + '</div>'
      + '文件夹是空的<br><span style="font-size:12px;color:#9aa8bd">一本有声书放一个子文件夹，里面放音频文件</span></div>';
    return;
  }

  box.innerHTML = shelf.list.map((b, i) => {
    const c = bookColor(b.name);
    const total = b.count || 0;
    const heardNo = b.heard >= 0 ? b.heard + 1 : 0;
    const pct = total ? Math.min(100, Math.round(heardNo / total * 100)) : 0;
    const done = total && heardNo >= total;
    let badge = '';
    if (done) badge = '<span class="book-badge done">✓ 听完</span>';
    else if (heardNo) badge = '<span class="book-badge">' + heardNo + (total ? '/' + total : '') + '</span>';
    return '<div class="book-card" onclick="openBook(' + i + ')">'
      + badge
      + '<div class="book-cv" style="background:linear-gradient(140deg,' + c[0] + ',' + c[1] + ')"><b>' + esc(b.name) + '</b></div>'
      + '<div class="book-name">' + esc(b.name) + '</div>'
      + '<div class="book-sub"><span class="bk-author">' + esc(b.author || '未知作者') + '</span>'
      + (total ? '<span class="bk-total"> · ' + total + ' 集</span>'
        : (b.single ? '' : '<span class="bk-total"> · 统计中</span>')) + '</div>'
      + '<div class="book-prog"><i style="width:' + pct + '%"></i></div>'
      + '</div>';
  }).join('');
}

/* ---------- 目录 ---------- */
function openBook(i) {
  const b = shelf.list[i];
  if (b) openBookById(b.id, b.name, b.single);
}

async function openBookById(id, name, single) {
  book = {
    id: id, name: name || '', single: !!single,
    raw: [], list: [], orderMap: {},
    idx: -1, heard: -1, progress: {},
    sortBy: book.sortBy, shown: CH_PAGE, pendingSeek: 0,
    openVols: {}, volInit: false, volFolded: false,
  };
  go('book');
  $('#bookTitle').textContent = name || '…';
  $('#bookMeta').textContent = '正在读取章节…';
  $('#bookResumeBtn').hidden = true;
  $('#chapterMore').hidden = true;
  $('#chapterList').innerHTML = '<div class="empty"><div class="spinner" style="margin:0 auto"></div></div>';

  try {
    const d = await api('/api/books/' + encodeURIComponent(id) + (single ? '?single=1' : ''));
    book.raw = d.chapters || [];
    book.progress = d.progress || {};
    book.heard = typeof book.progress.index === 'number' ? book.progress.index : -1;
    book.orderMap = {};
    book.raw.forEach((c, k) => { book.orderMap[c.id] = k; });
    applySort();
    renderChapters();

    if (book.raw.length) {
      $('#bookResumeBtn').hidden = false;
      $('#bookResumeBtn').textContent = book.heard >= 0
        ? '继续听 · 第 ' + (book.heard + 1) + ' 集' : '从头开始听';
    }
  } catch (e) {
    $('#chapterList').innerHTML = '<div class="empty"><div class="big">' + ICON_WARN + '</div>' + esc(e.message) + '</div>';
    $('#bookMeta').textContent = '';
  }
}

function applySort() {
  const curId = book.list[book.idx] ? book.list[book.idx].id : '';
  if (book.sortBy === 'time') {
    book.list = book.raw.slice().sort((a, b) => String(b.mtime || '').localeCompare(String(a.mtime || '')));
  } else {
    book.list = book.raw.slice();
  }
  if (curId) {
    const k = book.list.findIndex((c) => c.id === curId);
    if (k >= 0) book.idx = k;
  }
}

function bookSort(by) {
  if (book.sortBy === by) return;
  book.sortBy = by;
  $$('#bookSeg .seg-btn').forEach((b) => b.classList.toggle('active', b.dataset.sort === by));
  applySort();
  renderChapters();
  toast(by === 'time' ? '按修改时间排序' : '按集数排序');
}

function rawIndexOf(ch) {
  const k = book.orderMap[ch.id];
  return (k === undefined || k === null) ? book.idx : k;
}

function currentBookId() {
  return (mode === 'book' && book.list[book.idx]) ? book.list[book.idx].id : '';
}

function chapterRow(i, curId) {
  const c = book.list[i];
  const isCur = c.id === curId;
  const ri = rawIndexOf(c);
  const heard = book.heard >= 0 && ri < book.heard && !isCur;
  const doing = book.heard >= 0 && ri === book.heard && !isCur;
  const num = isCur ? '▶' : (heard ? '✓' : (c.label || '·'));
  let cls = isCur ? 'cur' : (heard ? 'heard' : (doing ? 'doing' : ''));
  if (c.playable === false) cls += ' bad';
  return '<div class="ch-row ' + cls + '" data-i="' + i + '" onclick="playChapter(' + i + ')">'
    + '<span class="ch-num">' + esc(String(num)) + '</span>'
    + '<span class="ch-name">' + esc(c.name) + '</span>'
    + '<span class="ch-size">' + fmtSize(c.size) + '</span>'
    + '</div>';
}

// 卷名里重复了书名的话，掐掉前面那截，手机上一行放得下
function shortVolName(vol) {
  const bn = book.name || '';
  if (!bn) return vol;
  const i = vol.indexOf(bn);
  if (i > 0 && i < 20) {
    const tail = vol.slice(i + bn.length).replace(/^[\s\-_·:：]+/, '');
    if (tail.length >= 2) return tail;
  }
  return vol;
}

function bookVolToggle(from) {
  const c = book.list[from];
  if (!c) return;
  const k = c.volume || ('seg:' + from);
  book.openVols[k] = !book.openVols[k];
  renderChapters();
}

function renderChapters() {
  const box = $('#chapterList');
  if (!book.list.length) {
    box.innerHTML = '<div class="empty"><div class="big">' + ICON_HEADPHONE + '</div>这本书里没有音频文件</div>';
    return;
  }
  const curId = currentBookId();

  // 按卷切段（已按卷号排好，同卷是连续的）
  let groups = [];
  for (let i = 0; i < book.list.length; i++) {
    const v = book.list[i].volume || '';
    const g = groups[groups.length - 1];
    if (!g || g.vol !== v) groups.push({ vol: v, from: i, to: i + 1 });
    else g.to = i + 1;
  }
  const hasRealVol = groups.some((g) => g.vol);

  // 云盘里没分卷的书（几百集全平铺）按 CH_SEG 集折一段，至少不用滑到底
  // 一百集以内就不切了，「50 + 10」那种碎段没意义
  if (!hasRealVol && book.list.length > CH_SEG * 2) {
    groups = [];
    const total = book.list.length;
    let i = 0;
    while (i < total) {
      let end = Math.min(total, i + CH_SEG);
      // 尾巴太短就并进前一段，别留个七八集的孤儿
      if (total - end < CH_SEG * 0.4) end = total;
      groups.push({ vol: '', seg: true, from: i, to: end });
      i = end;
    }
  }

  // 只有一个卷、或者集数不多的时候折叠没意义，平铺就行
  // 按修改时间排的时候卷会被打散，也平铺
  const folded = book.sortBy !== 'time'
    && (hasRealVol ? groups.filter((g) => g.vol).length > 1 : groups.length > 1);
  book.volFolded = folded;

  // 一律先折起来；只有在放这本的时候，才把正在播的那一卷打开
  if (!book.volInit) {
    book.volInit = true;
    if (folded && mode === 'book' && book.idx >= 0) {
      const gi = groups.find((g) => g.from <= book.idx && book.idx < g.to);
      if (gi) book.openVols[gi.vol || ('seg:' + gi.from)] = true;
    }
  }

  let html = '';
  if (!folded) {
    const n = Math.min(book.shown, book.list.length);
    for (let i = 0; i < n; i++) html += chapterRow(i, curId);
  } else {
    for (const g of groups) {
      if (!g.vol && !g.seg) {
        // 收了卷的书里，卷外那些零散音频直接平铺在最上面
        for (let i = g.from; i < g.to; i++) html += chapterRow(i, curId);
        continue;
      }
      const gkey = g.vol || ('seg:' + g.from);
      const gtitle = g.vol ? shortVolName(g.vol) : ('第 ' + (g.from + 1) + ' – ' + g.to + ' 集');
      const open = !!book.openVols[gkey];
      let heardN = 0, curIn = false, hereIn = false;
      for (let i = g.from; i < g.to; i++) {
        const ri = rawIndexOf(book.list[i]);
        if (book.heard >= 0 && ri < book.heard) heardN++;
        if (book.heard >= 0 && ri === book.heard) hereIn = true;
        if (book.list[i].id === curId) curIn = true;
      }
      html += '<div class="ch-vol' + (open ? ' open' : '') + (curIn ? ' live' : '')
        + (!open && hereIn ? ' here' : '') + '"'
        + ' onclick="bookVolToggle(' + g.from + ')">'
        + '<span class="ch-caret">' + (open ? '▾' : '▸') + '</span>'
        + '<span class="ch-vol-t" title="' + esc(g.vol || gtitle) + '">' + esc(gtitle) + '</span>'
        + '<span class="ch-vol-n">' + (g.to - g.from) + ' 集</span>'
        + (heardN ? '<span class="ch-vol-p">已听 ' + heardN + '</span>' : '')
        + '</div>';
      if (open) {
        for (let i = g.from; i < g.to; i++) html += chapterRow(i, curId);
      }
    }
  }
  box.innerHTML = html;
  const n = folded ? book.list.length : Math.min(book.shown, book.list.length);
  $('#chapterMore').hidden = folded || n >= book.list.length;

  // 整本书都是浏览器解不了的格式时，顶部给一句明白话
  const warn = $('#bookWarn');
  if (warn) {
    const allBad = book.list.length > 0 && book.list.every((c) => c.playable === false);
    warn.hidden = !allBad;
    if (allBad) {
      warn.innerHTML = '这 <b>' + book.list.length + '</b> 集是 <b>WMA / APE</b> 格式，浏览器解不了，'
        + '转成 <b>MP3</b> 或 <b>M4A</b> 才能听。';
    }
  }

  let meta = book.list.length + ' 集';
  const volN = groups.filter((g) => g.vol).length;
  if (folded && volN) meta += ' · ' + volN + ' 卷';
  if (book.progress && book.progress.updated) {
    const ri = book.progress.index;
    if (typeof ri === 'number') meta += ' · 已听到第 ' + (ri + 1) + ' 集';
  }
  $('#bookMeta').textContent = meta;
}

function chapterMore() {
  if (book.shown >= book.list.length) return;
  book.shown = Math.min(book.list.length, book.shown + CH_PAGE);
  renderChapters();
}

function bookJump() {
  const el = $('#jumpInput');
  const v = parseInt(el.value, 10);
  if (!v || v < 1) { toast('先填第几集', true); return; }
  el.value = '';

  // 按「集号」找，不能按行号：书名里可能混着旁说/片花，行号和集号对不上
  let idx = book.list.findIndex((c) => c.num === v);
  if (idx < 0) idx = book.list.findIndex((c) => c.label === String(v));
  if (idx < 0) {
    const maxNum = book.list.reduce((m, c) => Math.max(m, c.num || 0), 0);
    toast('没找到第 ' + v + ' 集（这本最大 ' + maxNum + '）', true);
    return;
  }
  if (idx + 10 > book.shown) {
    book.shown = Math.min(book.list.length, idx + 20);
  }
  playChapter(idx, 0);
  setTimeout(() => {
    const row = document.querySelector('.ch-row[data-i="' + idx + '"]');
    if (row) row.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, 80);
}

/* ---------- 播放 ---------- */
function playChapter(i, startPos) {
  if (!book.list.length) return;
  const n = book.list.length;
  const idx = ((i % n) + n) % n;
  if (mode === 'book' && book.idx >= 0 && book.list[book.idx]) saveProgress();
  const ch = book.list[idx];
  if (ch.playable === false) {
    toast('这个格式（WMA / APE）手机浏览器解不了，转成 MP3 或 M4A 才能听', true);
    return;
  }

  mode = 'book';
  book.idx = idx;
  if (book.volFolded) {
    // 切到别的卷/别的段时，把它展开，不然看不到正在播的这集
    book.openVols[ch.volume || ('seg:' + (idx - (idx % CH_SEG)))] = true;
  }
  book.pendingSeek = Math.max(0, startPos || 0);
  book.retried = false;
  lastSaveAt = 0;

  const a = audio();
  a.pause();
  a.src = '/api/play/' + encodeURIComponent(ch.id) + '?t=' + Date.now();
  a.playbackRate = bookSpeed;
  a.play().catch(() => {});
  a.volume = 1;

  updateBookUI();
  renderChapters();
  refreshPlayerUI();
}

function updateBookUI() {
  const ch = book.list[book.idx];
  if (!ch) return;
  $('#bpBookName').textContent = book.name;
  $('#bpTitle').textContent = ch.name;
  $('#bpSub').textContent = ch.volume ? (book.name + ' · ' + ch.volume) : book.name;
  const c = bookColor(book.name);
  const cv = $('#bpCover');
  cv.style.background = 'linear-gradient(140deg,' + c[0] + ',' + c[1] + ')';
  cv.textContent = (book.name || '书').slice(0, 2);
  $('#bpSpeed').textContent = speedText();
}

function bookToggle() {
  const a = audio();
  if (mode !== 'book' || !a.src) {
    bookResume();
    return;
  }
  if (a.paused) a.play().catch(() => {}); else a.pause();
}

function bookPrev() {
  const a = audio();
  if (a.currentTime > 5) { a.currentTime = 0; return; }  // 先回本集开头
  if (book.idx <= 0) toast('已经是第一集了');
  playChapter(book.idx - 1, 0);
}

function bookNext() {
  if (book.idx >= book.list.length - 1) { toast('已经是最后一集了'); return; }
  playChapter(book.idx + 1, 0);
}

function bookOnEnded() {
  if (bookLoopOn) { playChapter(book.idx, 0); return; }
  if (book.idx >= book.list.length - 1) {
    toast('这本书听完了');
    setPlayIcon(false);
    bookSetIcon(false);
    saveProgress();
    return;
  }
  playChapter(book.idx + 1, 0);
}

function bookOnError() {
  const ch = book.list[book.idx];
  if (!ch) return;
  if (!book.retried) {
    book.retried = true;
    const a = audio();
    a.src = '/api/play/' + encodeURIComponent(ch.id) + '?t=' + Date.now();
    a.play().catch(() => {});
    return;
  }
  book.retried = false;
  toast('这一集播放失败，可能文件已被删除', true);
  setPlayIcon(false);
  bookSetIcon(false);
}

function bookOnMeta() {
  const a = audio();
  // durationchange 会早于 loadedmetadata 触发，此时设 currentTime 会失败。
  // 所以必须等 readyState 足够、且时长有效才跳转；失败就留着下次再试。
  if (book.pendingSeek > 0 && a.readyState >= 1
      && isFinite(a.duration) && a.duration > 0) {
    const target = Math.min(book.pendingSeek, Math.max(0, a.duration - 1));
    try {
      a.currentTime = target;
      book.pendingSeek = 0;
    } catch (e) { /* 下次事件再试 */ }
  }
  a.playbackRate = bookSpeed;
  if (a.duration) $('#bpDur').textContent = fmtTime(a.duration);
}

function bookTick() {
  const a = audio();
  if (a.duration) {
    const pct = (a.currentTime / a.duration * 100) + '%';
    $('#bpBar').style.width = pct;
    $('#mpBar').style.width = pct;
    $('#bpCur').textContent = fmtTime(a.currentTime);
    $('#bpDur').textContent = fmtTime(a.duration);
  }
  if (Date.now() - lastSaveAt > 10000) {
    lastSaveAt = Date.now();
    saveProgress();
  }
}

function bookSetIcon(playing) {
  const p = $('#bpIconPlay'), q = $('#bpIconPause');
  if (p) p.style.display = playing ? 'none' : '';
  if (q) q.style.display = playing ? '' : 'none';
}

function bookBack15() {
  const a = audio();
  a.currentTime = Math.max(0, a.currentTime - 15);
}

function bpSeek(ev) {
  const a = audio();
  if (!a.duration) return;
  const bar = ev.currentTarget.querySelector('.bar');
  const r = bar.getBoundingClientRect();
  const pct = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
  a.currentTime = pct * a.duration;
}

function applySpeed() {
  const a = audio();
  if (mode === 'book') a.playbackRate = bookSpeed;
  const el = $('#bpSpeed');
  if (el) el.textContent = speedText();
  try { localStorage.setItem(SPEED_KEY, String(bookSpeed)); } catch (e) { /* 忽略 */ }
}

function cycleSpeed() {
  const i = SPEEDS.indexOf(bookSpeed);
  bookSpeed = SPEEDS[(i + 1) % SPEEDS.length];
  applySpeed();
  refreshPlayerUI();
  toast('倍速 ' + speedText());
}

function toggleBookLoop() {
  bookLoopOn = !bookLoopOn;
  $('#bpLoop').classList.toggle('on', bookLoopOn);
  toast(bookLoopOn ? '本集循环：开' : '本集循环：关');
}

function cycleTimer() {
  timerIdx = (timerIdx + 1) % TIMER_MIN.length;
  const m = TIMER_MIN[timerIdx];
  clearTimeout(timerHandle);
  const btn = $('#bpTimer');
  const hint = $('#bpHint');

  if (!m) {
    btn.classList.remove('on');
    btn.textContent = '定时关闭';
    hint.textContent = '';
    return;
  }
  btn.classList.add('on');
  btn.textContent = m + ' 分钟';
  hint.textContent = m + ' 分钟后自动暂停';
  timerHandle = setTimeout(() => {
    fadeOutPause();
    btn.classList.remove('on');
    btn.textContent = '定时关闭';
    hint.textContent = '已按时暂停';
    timerIdx = 0;
  }, m * 60000);
}

function resetBookTimer() {
  clearTimeout(timerHandle);
  timerHandle = null;
  timerIdx = 0;
  const b = $('#bpTimer');
  if (b) { b.classList.remove('on'); b.textContent = '定时关闭'; }
  const h = $('#bpHint');
  if (h) h.textContent = '';
}

function fadeOutPause() {
  const a = audio();
  const v0 = a.volume;
  let v = v0;
  const step = setInterval(() => {
    v -= 0.1;
    if (v <= 0) {
      clearInterval(step);
      a.pause();
      a.volume = v0;
    } else {
      a.volume = v;
    }
  }, 120);
}

/* ---------- 进度 ---------- */
function saveProgress() {
  if (mode !== 'book' || !book.id || book.idx < 0) return;
  const ch = book.list[book.idx];
  if (!ch) return;
  const a = audio();
  const body = {
    book_id: book.id,
    index: Math.max(0, rawIndexOf(ch)),
    file_id: ch.id,
    pos: a.currentTime || 0,
    dur: a.duration || 0,
    count: book.list.length,
  };
  // 本地也留一份，断网时书架仍能显示进度
  const local = cacheRead('listen.progress.v1') || {};
  local[book.id] = body;
  cacheWrite('listen.progress.v1', local);

  fetch('/api/progress', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).catch(() => {});

  const b = shelf.list.find((x) => x.id === book.id);
  if (b) { b.heard = body.index; b.count = book.list.length; }
  book.heard = body.index;
}

function bookResume() {
  if (!book.list.length) {
    if (book.id) openBookById(book.id, book.name, book.single);
    return;
  }
  let idx = book.heard >= 0 ? book.heard : 0;
  let pos = 0;
  const p = book.progress || {};
  if (p.file_id) {
    const k = book.list.findIndex((c) => c.id === p.file_id);
    if (k >= 0) { idx = k; pos = p.pos || 0; }
  }
  playChapter(idx, pos);
  toggleBookPanel(true);
}

/* ---------- 面板 ---------- */
function toggleBookPanel(open) {
  const el = $('#bookPanel');
  const on = (typeof open === 'boolean') ? open : el.hidden;
  el.hidden = !on;
  document.body.style.overflow = on ? 'hidden' : '';
  if (on) updateBookUI();
}

function openBookFromPanel() {
  toggleBookPanel(false);
  if (book.list.length) {
    go('book');
    if (book.idx + 20 > book.shown) {
      book.shown = Math.min(book.list.length, book.idx + 40);
    }
    renderChapters();
    setTimeout(() => {
      const rows = document.querySelectorAll('.ch-row');
      const row = rows[book.idx];
      if (row) row.scrollIntoView({ block: 'center' });
    }, 120);
  } else if (book.id) {
    openBookById(book.id, book.name, book.single);
  } else {
    go('books');
  }
}

/* ---------- 设置：听书目录 ---------- */
function editBookFolder() {
  openRename({
    heading: '听书目录',
    tip: '云盘根目录下的文件夹名。里面一本有声书放一个子文件夹。',
    value: shelf.root || '有声书',
    okText: '保存',
    onOk: async (v) => {
      const d = await api('/api/bookfolder', {
        method: 'POST',
        body: JSON.stringify({ name: v }),
      });
      shelf = { root: d.name, exists: false, list: [], ts: 0 };
      cacheWrite(SHELF_KEY, shelf);
      toast('已改为「' + d.name + '」');
      renderShelf();
      loadBooks(true);
    },
  });
}

function renderBookFolder() {
  const box = $('#setBookBody');
  if (!box) return;
  if (!shelf.root) {
    box.innerHTML = '<div class="muted">读取中…</div>';
    api('/api/bookfolder')
      .then((d) => { shelf.root = d.name || '有声书'; renderBookFolder(); })
      .catch(() => { box.innerHTML = '<div class="muted">连接云盘后可设置</div>'; });
    return;
  }
  if (!nameOrderLoaded) {
    api('/api/bookname').then((d) => {
      nameOrder = d.order || 'auto';
      nameOrderLoaded = true;
      renderBookFolder();
    }).catch(() => { nameOrderLoaded = true; nameOrder = 'auto'; });
  }
  const cur = nameOrder || 'auto';
  const opt = (v, label) => '<button class="seg-btn ' + (cur === v ? 'active' : '')
    + '" onclick="setNameOrder(\'' + v + '\')">' + label + '</button>';

  box.innerHTML = '<div class="set-row">'
    + '<div><b>「' + esc(shelf.root || '有声书') + '」</b>'
    + '<span class="muted">云盘根目录下，一本一个子文件夹</span></div>'
    + '<button class="btn ghost sm" onclick="editBookFolder()">更改</button>'
    + '</div>'
    + '<div class="set-row" style="margin-top:18px">'
    + '<div><b>文件夹命名习惯</b>'
    + '<span class="muted">叫「A - B」时，哪个是书名</span></div></div>'
    + '<div class="seg book-seg">' + opt('auto', '自动认') + opt('title', '书名在前') + opt('author', '作者在前') + '</div>'
    + '<div class="name-hint">自动认：看哪一边像人名（以常见姓氏开头、2~4 字）。两边都不像时按「书名在前」。</div>';
}

async function setNameOrder(order) {
  try {
    await api('/api/bookname', { method: 'POST', body: JSON.stringify({ order: order }) });
    nameOrder = order;
    renderBookFolder();
    shelf = { root: shelf.root, exists: shelf.exists, list: [], ts: 0 };
    cacheWrite(SHELF_KEY, shelf);
    loadBooks(true);
    toast('已切换为「' + ({ auto: '自动认', title: '书名在前', author: '作者在前' }[order] || order) + '」');
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------- 转码 ---------- */
let cv = { data: null, timer: null, sel: new Set(), fold: new Set(), scanAt: -1, busy: false };

function cvEnter() {
  cvLoad(true);
  clearInterval(cv.timer);
  cv.timer = setInterval(() => {
    if ($('#page-convert').hidden) { clearInterval(cv.timer); return; }
    cvLoad(false);
  }, 1600);
}

async function cvLoad(force) {
  if (cv.busy) return;
  cv.busy = true;
  try {
    const d = await api('/api/convert/status');
    cv.data = d;
    cvRenderTop();
    cvRenderJobs();
    const scan = d.scan || {};
    // 扫描的 at 只在开始时写一次，光看它会在「跑完」那一刻漏掉重绘，
    // 于是页面一直停在「已检查 20/21」。把状态和进度一起纳入比较。
    const key = [scan.at || 0, scan.state || '', scan.done || 0, scan.total || 0, (scan.books || []).length].join('|');
    if (key !== cv.scanAt) {
      cv.scanAt = key;
      cvRenderBooks();
    } else if (scan.state === 'run') {
      cvRenderBooks();
    }
  } catch (e) {
    $('#cvStat').textContent = e.message;
    if (e.status === 401) $('#cvStat').innerHTML = '还没连天翼云盘，先去<a href="javascript:openCloud()">扫码登录</a>';
  } finally {
    cv.busy = false;
  }
}

function cvJobsMap() {
  const m = {};
  ((cv.data && cv.data.jobs) || []).forEach((j) => { m[j.id] = j; });
  return m;
}

function cvPending() {
  const jobs = cvJobsMap();
  const out = [];
  (((cv.data || {}).scan || {}).books || []).forEach((b) => {
    (b.pending || []).forEach((f) => {
      if (!jobs[f.id]) out.push(f);
    });
  });
  return out;
}

function cvRenderTop() {
  const d = cv.data;
  if (!d) return;
  $('#cvRoot').textContent = d.root ? '「' + d.root + '」里的书' : '';
  const cfg = d.cfg || {};
  $$('#cvFmtSeg .seg-btn').forEach((b) => b.classList.toggle('active', b.dataset.fmt === cfg.format));
  const sel = $('#cvRate');
  if (sel && document.activeElement !== sel) sel.value = String(cfg.bitrate || 96);

  const env = $('#cvEnv');
  if (!d.ffmpeg) {
    env.className = 'cv-env bad';
    env.textContent = '这台机器没装 ffmpeg，转不了';
  } else {
    env.className = 'cv-env ok';
    env.textContent = 'ffmpeg 就绪 · 临时盘剩余 ' + fmtSize(d.disk_free);
  }

  const pause = $('#cvPause');
  if (d.paused) {
    pause.hidden = false;
    pause.innerHTML = `<b>队列已暂停</b> · ${esc(d.paused)}
      <button class="mini-btn" onclick="cvResume()">继续转码</button>`;
  } else {
    pause.hidden = true;
  }

  const scan = d.scan || {};
  const pend = cvPending().length;
  const stat = d.stat || {};
  let txt = '';
  if (!d.logged_in) txt = '还没登录云盘';
  else if (scan.state === 'run') txt = scan.msg || '扫描中…';
  else if (scan.state === 'idle') txt = '还没扫描';
  else if (scan.err) txt = scan.err;
  else txt = `待转 <b>${pend}</b> 集 · 已完成 <b>${stat.done || 0}</b> · 失败 <b>${stat.fail || 0}</b>`;
  $('#cvStat').innerHTML = txt + (cv.sel.size ? ` · 已选 ${cv.sel.size}` : '');

  const badge = $('#cvBadge');
  const live = (stat.wait || 0) + (stat.run || 0);
  if (live) { badge.hidden = false; badge.textContent = live; }
  else badge.hidden = true;
}

function cvRenderJobs() {
  const box = $('#cvJobs');
  const jobs = ((cv.data || {}).jobs) || [];
  if (!jobs.length) { box.innerHTML = ''; return; }
  const live = jobs.filter((j) => j.state === 'run' || j.state === 'wait');
  const done = jobs.filter((j) => j.state === 'done').slice(-12);
  const fail = jobs.filter((j) => j.state === 'fail');
  const row = (j) => {
    const pct = Math.round((j.pct || 0) * 100);
    const cls = j.state === 'done' ? 'ok' : j.state === 'fail' ? 'bad' : j.state === 'run' ? 'run' : '';
    let right = '';
    if (j.state === 'run') right = `<span class="cv-job-stage">${esc(j.stage || '处理中')} ${pct}%</span>`;
    else if (j.state === 'wait') right = '<span class="cv-job-stage">排队中</span>';
    else if (j.state === 'done') right = '<span class="cv-job-stage ok">已转成 ' + esc(j.out || '') + '</span>';
    else right = '<span class="cv-job-stage bad">' + esc(j.msg || '失败') + '</span>';
    const acts = j.state === 'done'
      ? `<button class="mini-btn" onclick="cvDelOrigin('${j.id}')">删原件</button>
         <button class="mini-btn" onclick="cvRemoveJob('${j.id}')">移除</button>`
      : j.state === 'fail'
        ? `<button class="mini-btn" onclick="cvRetry('${j.id}')">重试</button>
           <button class="mini-btn" onclick="cvRemoveJob('${j.id}')">移除</button>`
        : '';
    return `<div class="cv-job ${cls}">
      <div class="cv-job-main">
        <span class="cv-job-name">${esc(j.name)}</span>${right}
      </div>
      ${j.state === 'run' ? `<div class="cv-job-bar"><i style="width:${pct}%"></i></div>` : ''}
      ${acts ? `<div class="cv-job-acts">${acts}</div>` : ''}
    </div>`;
  };
  box.innerHTML =
    (live.length ? `<div class="cv-group"><div class="cv-group-t">正在处理</div>${live.map(row).join('')}</div>` : '') +
    (fail.length ? `<div class="cv-group"><div class="cv-group-t">失败 ${fail.length}</div>${fail.map(row).join('')}
      <button class="btn ghost sm" onclick="cvRetryAll()">全部重试</button></div>` : '') +
    (done.length ? `<div class="cv-group"><div class="cv-group-t">最近完成</div>${done.map(row).join('')}
      <button class="btn ghost sm" onclick="cvClearDone()">清空这些记录</button></div>` : '');
}

function cvRenderBooks() {
  const box = $('#cvBooks');
  const d = cv.data || {};
  const scan = d.scan || {};
  if (scan.state === 'idle') return;
  if (scan.state === 'run') {
    box.innerHTML = `<div class="empty"><div class="spinner" style="margin:0 auto"></div>
      <div style="margin-top:12px">${esc(scan.msg || '扫描中…')}</div></div>`;
    return;
  }
  const jobs = cvJobsMap();
  const books = (scan.books || []).map((b) => ({
    ...b,
    pending: (b.pending || []).filter((f) => !jobs[f.id]),
  })).filter((b) => b.pending.length || b.done || (b.cleanup || []).length);
  if (!books.length) {
    box.innerHTML = `<div class="empty">
      <div class="big"><svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"></circle><path d="M8 12.5l2.6 2.6L16 9.5"></path></svg></div>
      没有需要转码的文件，都是浏览器能直接播的</div>`;
    return;
  }
  box.innerHTML = books.map((b) => {
    const open = !cv.fold.has(b.id);
    const rows = b.pending.map((f) => {
      const tag = (f.ext || '').replace('.', '').toUpperCase();
      const kind = f.kind === 'video' ? ' <i class="cv-kind">视频</i>' : '';
      const bad = f.fail ? ` <i class="cv-kind bad" title="${esc(f.fail)}">上次失败</i>` : '';
      return `<label class="cv-file">
        <input type="checkbox" data-id="${f.id}" ${cv.sel.has(f.id) ? 'checked' : ''}
               onchange="cvSel('${f.id}', this.checked)">
        <span class="cv-fname">${esc(f.name)}</span>${kind}${bad}
        <span class="cv-ext">${tag}</span>
        <span class="cv-fsize">${fmtSize(f.size)}</span>
      </label>`;
    }).join('');
    const clean = b.cleanup || [];
    const cleanRows = clean.map((f) => `<div class="cv-file clean">
        <span class="cv-fname">${esc(f.name)}</span>
        <span class="cv-tag done">已转好</span>
        <span class="cv-fsize">${fmtSize(f.size)}</span>
        <button class="mini-btn" onclick="event.stopPropagation();cvDelOrigin('${f.id}')">删原件</button>
      </div>`).join('');
    const body = rows + (clean.length
      ? `<div class="cv-sub">
           <span>原件还在 ${clean.length} 个，删掉不影响播放</span>
           <button class="mini-btn" onclick="event.stopPropagation();cvDelOriginBook('${b.id}')">全删原件</button>
         </div>${cleanRows}`
      : '');
    return `<div class="cv-book">
      <div class="cv-book-head" onclick="cvFold('${b.id}')">
        <span class="cv-caret">${open ? '▾' : '▸'}</span>
        <b>${esc(b.name)}</b>
        <span class="muted cv-author">${esc(b.author || '')}</span>
        ${b.pending.length ? `<span class="cv-tag">待转 ${b.pending.length}</span>` : ''}
        ${b.done ? `<span class="cv-tag done">已转 ${b.done}</span>` : ''}
      </div>
      ${open ? `<div class="cv-files">${body || '<div class="muted" style="padding:10px 12px">这本已经转完了</div>'}</div>` : ''}
    </div>`;
  }).join('');
}

function cvFold(id) {
  if (cv.fold.has(id)) cv.fold.delete(id); else cv.fold.add(id);
  cvRenderBooks();
}

function cvSel(id, on) {
  if (on) cv.sel.add(id); else cv.sel.delete(id);
  cvRenderTop();
}

function cvSelectAll() {
  const pend = cvPending();
  if (cv.sel.size >= pend.length) cv.sel.clear();
  else pend.forEach((f) => cv.sel.add(f.id));
  cvRenderBooks();
  cvRenderTop();
}

function cvItems() {
  const pend = cvPending();
  return pend.filter((f) => cv.sel.has(f.id)).map((f) => ({
    id: f.id, name: f.name, size: f.size, folder: f.folder, book: f.book || '',
  }));
}

async function cvStart() {
  const items = cvItems();
  if (!items.length) { toast('先勾选要转的文件', true); return; }
  try {
    const r = await api('/api/convert/start', { method: 'POST', body: JSON.stringify({ items: items }) });
    cv.sel.clear();
    toast('已加入队列 ' + r.added + ' 集，慢慢跑');
    cvLoad(true);
  } catch (e) { toast(e.message, true); }
}

async function cvScan() {
  try {
    const r = await api('/api/convert/scan', { method: 'POST', body: JSON.stringify({}) });
    if (r && r.ok === false) { toast(r.msg || '正在扫描', true); return; }
    cv.scanAt = -1;
    cvLoad(true);
  } catch (e) { toast(e.message, true); }
}

async function cvRetry(id) {
  try { await api('/api/convert/retry', { method: 'POST', body: JSON.stringify({ ids: [id] }) }); cvLoad(true); }
  catch (e) { toast(e.message, true); }
}

async function cvRetryAll() {
  try { await api('/api/convert/retry', { method: 'POST', body: JSON.stringify({ ids: [] }) }); cvLoad(true); }
  catch (e) { toast(e.message, true); }
}

async function cvResume() {
  // 暂停时剩下的任务还在队列里，点一下从那一集接着跑
  try {
    await api('/api/convert/resume', { method: 'POST', body: '{}' });
    toast('继续转码');
    cvLoad(true);
  } catch (e) { toast(e.message, true); }
}

async function cvRemoveJob(id) {
  try { await api('/api/convert/remove', { method: 'POST', body: JSON.stringify({ ids: [id] }) }); cvLoad(true); }
  catch (e) { toast(e.message, true); }
}

async function cvClearDone() {
  try { await api('/api/convert/clear', { method: 'POST', body: '{}' }); cvLoad(true); }
  catch (e) { toast(e.message, true); }
}

async function cvDelOriginBook(bid) {
  const b = (((cv.data || {}).scan || {}).books || []).find((x) => x.id === bid) || {};
  const ids = (b.cleanup || []).map((c) => c.id);
  if (!ids.length) return;
  if (!confirm(`确定删掉「${b.name}」的 ${ids.length} 个原文件？\n转好的文件会留着，原件删了没法恢复。`)) return;
  try {
    const r = await api('/api/convert/delete-origin', { method: 'POST', body: JSON.stringify({ ids: ids }) });
    toast(`已删 ${r.ok} 个` + ((r.bad || []).length ? `，${r.bad.length} 个没删掉` : ''), !r.ok);
    cvScan();
  } catch (e) { toast(e.message, true); }
}

async function cvDelOrigin(id) {
  if (!confirm('确定删掉云盘里的原文件？\n转好的 m4a 会留着，原文件删了就没法恢复了。')) return;
  try {
    const r = await api('/api/convert/delete-origin', { method: 'POST', body: JSON.stringify({ ids: [id] }) });
    toast(r.ok ? '原文件已删除' : '删除失败', !r.ok);
    cvLoad(true);
  } catch (e) { toast(e.message, true); }
}

async function cvSetFmt(fmt) {
  try {
    await api('/api/convert/cfg', { method: 'POST', body: JSON.stringify({ format: fmt }) });
    cvLoad(true);
    toast('输出格式：' + fmt.toUpperCase());
  } catch (e) { toast(e.message, true); }
}

async function cvSetRate(v) {
  try {
    await api('/api/convert/cfg', { method: 'POST', body: JSON.stringify({ bitrate: parseInt(v, 10) }) });
    cvLoad(true);
  } catch (e) { toast(e.message, true); }
}

/* ---------- 滚动加载更多 ---------- */
window.addEventListener('scroll', () => {
  if ($('#page-book').hidden) return;
  if (book.volFolded) return;
  if (book.shown >= book.list.length) return;
  const now = Date.now();
  if (now - scrollStamp < 400) return;
  const doc = document.documentElement;
  if (doc.scrollTop + window.innerHeight > doc.scrollHeight - 500) {
    scrollStamp = now;
    chapterMore();
  }
}, { passive: true });

$('#bookPanel').addEventListener('click', (e) => {
  if (e.target.id === 'bookPanel') toggleBookPanel(false);
});

$('#bpSpeed').textContent = speedText();

(async function init() {
  renderHot();
  bindAudio();
  // 先把本地缓存吐出来，页面一打开就有内容，不用等网络
  loadSearchCache();
  loadTasksCache();
  loadStatus();     // 不 await，避免挡住上面的渲染
  pollTasks();
})();
