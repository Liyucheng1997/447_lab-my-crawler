/* 我的爬虫 · 前端逻辑。抓来的内容都当作不可信文本，统一经 esc() 再放进页面。 */
const $ = (s) => document.querySelector(s);
const TYPES = [
  ['image', '图片'], ['video', '视频'], ['audio', '音频'], ['file', '文件'],
  ['link', '链接'], ['text', '正文'], ['table', '表格'],
];
const LABEL = Object.fromEntries([...TYPES, ['custom', '自定义']]);
const DOWNLOADABLE = new Set(['image', 'video', 'audio', 'file']);
const PAGE_SIZE = 240;

const state = {
  types: new Set(TYPES.map((t) => t[0])),
  depth: 0,
  job: null,        // 当前任务 id
  summary: null,
  items: [],
  tab: 'image',
  tabPicked: false, // 用户手动点过页签后，不再自动跳
  selected: new Set(),
  shown: PAGE_SIZE,
  dims: {},         // 图片 id -> 宽度
  timer: null,
  info: {},
};

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtSize = (n) => !n ? '' : n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(0) + ' KB' : n < 1073741824 ? (n / 1048576).toFixed(1) + ' MB' : (n / 1073741824).toFixed(2) + ' GB';
const host = (u) => { try { return new URL(u).host; } catch { return ''; } };
const proxied = (it) => `/api/proxy?url=${encodeURIComponent(it.url)}&ref=${encodeURIComponent(it.page)}`;

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + (kind ? 'toast-' + kind : '');
  el.textContent = msg;
  $('#toasts').append(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 400); }, 2800);
}

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = '请求失败 ' + res.status;
    try { const d = (await res.json()).detail; if (typeof d === 'string') msg = d; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

/* ───────── 顶部选项 ───────── */
function renderChips() {
  $('#typeChips').innerHTML = TYPES.map(([k, name]) =>
    `<button type="button" class="chip ${state.types.has(k) ? 'active' : ''}" data-k="${k}" aria-pressed="${state.types.has(k)}">${name}</button>`).join('');
}
$('#typeChips').addEventListener('click', (e) => {
  const k = e.target.dataset.k;
  if (!k) return;
  state.types.has(k) ? state.types.delete(k) : state.types.add(k);
  renderChips();
});
$('#depthSeg').addEventListener('click', (e) => {
  if (e.target.dataset.v === undefined) return;
  state.depth = +e.target.dataset.v;
  for (const b of $('#depthSeg').children) b.classList.toggle('active', b === e.target);
});

const SUN = '<svg class="ico" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON = '<svg class="ico" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z"/></svg>';
function paintTheme() { $('#themeBtn').innerHTML = document.documentElement.dataset.theme === 'dark' ? SUN : MOON; }
$('#themeBtn').addEventListener('click', () => {
  const dark = document.documentElement.dataset.theme !== 'dark';
  if (dark) document.documentElement.dataset.theme = 'dark'; else delete document.documentElement.dataset.theme;
  try { localStorage.setItem('theme', dark ? 'dark' : 'light'); } catch {}
  paintTheme();
});

/* ───────── 抓取 ───────── */
$('#scanForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = $('#url').value.trim();
  if (!url) { $('#url').focus(); return toast('先填一个网址', 'error'); }
  if (!state.types.size && !$('#selector').value.trim()) return toast('至少选一种要抓的内容', 'error');
  try {
    const { id } = await api('/api/scan', {
      url, types: [...state.types], depth: state.depth,
      max_pages: +$('#maxPages').value || 20, delay: +$('#delay').value || 0.5,
      same_site: $('#sameSite').checked, robots: $('#robots').checked,
      render: $('#render').checked, selector: $('#selector').value.trim(),
    });
    Object.assign(state, { job: id, items: [], summary: null, selected: new Set(), shown: PAGE_SIZE, dims: {}, tabPicked: false });
    $('#statusCard').hidden = false;
    $('#resultCard').hidden = false;
    $('#view').innerHTML = '';
    clearTimeout(state.timer);
    poll();
  } catch (err) { toast(err.message, 'error'); }
});
$('#stopBtn').addEventListener('click', () => state.job && api(`/api/jobs/${state.job}/stop`, {}).catch(() => {}));
$('#pagesToggle').addEventListener('click', () => { $('#pagesList').hidden = !$('#pagesList').hidden; });

async function poll() {
  const id = state.job;
  let s;
  try { s = await api(`/api/jobs/${id}?offset=${state.items.length}`); }
  catch (err) {
    $('#statusDot').className = 'dot bad';
    $('#statusText').textContent = err.message;
    return;
  }
  if (id !== state.job) return;
  const fresh = s.items;
  delete s.items;
  state.summary = s;
  state.items.push(...fresh);
  renderStatus();
  renderTabs();
  if (!state.tabPicked && !(s.counts[state.tab] > 0)) {
    const first = Object.keys(LABEL).find((k) => s.counts[k] > 0);
    if (first) { state.tab = first; renderTabs(); renderView(); }
  }
  if (fresh.some((it) => it.type === state.tab) || (s.status !== 'running' && !state.items.length)) renderView();
  if (s.status === 'running') state.timer = setTimeout(poll, 700);
}

function renderStatus() {
  const s = state.summary;
  const map = { running: ['run', '正在抓取'], done: ['ok', '抓取完成'], stopped: ['warn', '已停止'], error: ['bad', '出错了'] };
  const [cls, text] = map[s.status];
  $('#statusDot').className = 'dot ' + cls;
  $('#statusText').textContent = text + (s.message ? ' · ' + s.message : '');
  const done = s.pages.filter((p) => p.status !== '抓取中').length;
  $('#statusMeta').textContent = `已抓 ${done} 页${s.queued ? ` · 排队 ${s.queued}` : ''} · 找到 ${s.total} 项 · ${s.elapsed} 秒`;
  $('#stopBtn').hidden = s.status !== 'running';
  $('#startBtn').disabled = s.status === 'running';
  $('#pagesList').innerHTML = s.pages.map((p) => `
    <div class="page-row">
      <a href="${esc(p.url)}" target="_blank" rel="noreferrer noopener" title="${esc(p.url)}">${esc(p.title || p.url)}</a>
      <span class="muted tabular">${p.found} 项</span>
      <span class="${p.status === '完成' || p.status === '抓取中' ? 'muted' : ''}" style="${p.status === '完成' || p.status === '抓取中' ? '' : 'color:var(--warning)'}">${esc(p.status)}</span>
    </div>`).join('');
  // 只有一页而且没抓到，直接把原因摆出来
  if (s.status !== 'running' && s.pages.length === 1 && s.pages[0].status !== '完成') {
    $('#statusText').textContent = text + ' · ' + s.pages[0].status;
    $('#statusDot').className = 'dot warn';
  }
}

function renderTabs() {
  const c = state.summary?.counts || {};
  const keys = Object.keys(LABEL).filter((k) => k !== 'custom' || c.custom);
  $('#tabs').innerHTML = keys.map((k) =>
    `<button class="tab ${k === state.tab ? 'active' : ''} ${c[k] ? '' : 'zero'}" data-k="${k}">${LABEL[k]}<span class="n">${c[k] || 0}</span></button>`).join('');
}
$('#tabs').addEventListener('click', (e) => {
  const b = e.target.closest('.tab');
  if (!b) return;
  state.tab = b.dataset.k;
  state.tabPicked = true;
  state.shown = PAGE_SIZE;
  renderTabs();
  renderView();
});

/* ───────── 结果 ───────── */
function currentItems() {
  const q = $('#filter').value.trim().toLowerCase();
  const min = state.tab === 'image' ? +$('#minSize').value : 0;
  return state.items.filter((it) => {
    if (it.type !== state.tab) return false;
    if (min && (state.dims[it.id] ?? Infinity) < min) return false;
    if (!q) return true;
    const hay = [it.url, it.name, it.note, it.data?.text, it.data?.title].join(' ').toLowerCase();
    return hay.includes(q);
  });
}

function renderView() {
  const list = currentItems();
  const tab = state.tab;
  const canDl = DOWNLOADABLE.has(tab);
  $('#dlBtn').hidden = $('#selectAll').hidden = !canDl;
  $('#minSize').hidden = tab !== 'image';
  $('#exportCsv').hidden = tab === 'text';
  const view = $('#view');
  if (!list.length) {
    const running = state.summary?.status === 'running';
    view.innerHTML = `<div class="empty">${running ? '正在找…' : state.items.some((i) => i.type === tab) ? '没有符合筛选条件的内容' : `这次没有找到${LABEL[tab]}`}</div>`;
    $('#moreWrap').hidden = true;
    updateSel();
    return;
  }
  const part = list.slice(0, state.shown);
  if (tab === 'image') view.innerHTML = `<div class="grid">${part.map(tile).join('')}</div>`;
  else if (canDl) view.innerHTML = part.map(mediaRow).join('');
  else if (tab === 'link') view.innerHTML = part.map(linkRow).join('');
  else if (tab === 'text') view.innerHTML = part.map(textDoc).join('');
  else if (tab === 'table') view.innerHTML = part.map(tableDoc).join('');
  else view.innerHTML = part.map(customRow).join('');
  $('#moreWrap').hidden = list.length <= state.shown;
  $('#moreBtn').textContent = `显示更多（还有 ${list.length - state.shown} 项）`;
  updateSel();
}

const sel = (it) => (state.selected.has(it.id) ? 'sel' : '');
const tile = (it) => `
  <div class="tile ${sel(it)}" data-id="${it.id}" tabindex="0" role="checkbox" aria-checked="${state.selected.has(it.id)}" title="${esc(it.note || it.name)}">
    <img loading="lazy" decoding="async" src="${esc(proxied(it))}" alt="${esc(it.note)}">
    <span class="check">✓</span>
    <a class="open" href="${esc(it.url)}" target="_blank" rel="noreferrer noopener" title="打开原图" aria-label="打开原图">↗</a>
    <span class="cap"><span class="ellip">${esc(it.name)}</span><span class="dim tabular">${state.dims[it.id] ? state.dims[it.id] + 'px' : ''}</span></span>
  </div>`;
const kindNote = (it) => it.kind === 'embed' ? '嵌入播放器，下载时交给 yt-dlp 解析' : it.ext === 'm3u8' || it.ext === 'mpd' ? '流媒体，下载时自动合并成一个文件' : '';
const mediaRow = (it) => `
  <div class="row ${sel(it)}" data-id="${it.id}">
    <input type="checkbox" ${state.selected.has(it.id) ? 'checked' : ''} aria-label="选择 ${esc(it.name)}">
    <span class="badge">${esc(it.kind === 'embed' ? '嵌入' : it.ext || LABEL[it.type])}</span>
    <div style="min-width:0"><div class="name ellip">${esc(it.note || it.name)}</div><div class="subline ellip">${esc(kindNote(it) || it.url)}</div></div>
    <div class="src subline ellip">来自 ${esc(host(it.page))}</div>
    <a href="${esc(it.url)}" target="_blank" rel="noreferrer noopener">打开</a>
  </div>`;
const linkRow = (it) => `
  <div class="row norow">
    <span class="badge">${esc(it.kind)}</span>
    <div class="name ellip">${esc(it.note || '（无文字）')}</div>
    <a class="ellip" href="${esc(it.url)}" target="_blank" rel="noreferrer noopener">${esc(it.url)}</a>
  </div>`;
const customRow = (it) => `
  <div class="row norow">
    <span class="badge">${esc(it.data.tag)}</span>
    <div class="name ellip" title="${esc(it.data.text)}">${esc(it.data.text || '（无文字）')}</div>
    ${it.data.link ? `<a class="ellip" href="${esc(it.data.link)}" target="_blank" rel="noreferrer noopener">${esc(it.data.link)}</a>` : `<span class="subline ellip">来自 ${esc(host(it.page))}</span>`}
  </div>`;
const textDoc = (it) => `
  <article class="doc">
    <div class="doc-head">
      <div style="flex:1;min-width:0"><h3>${esc(it.data.title || '（无标题）')}</h3>
      <div class="meta ellip"><a href="${esc(it.page)}" target="_blank" rel="noreferrer noopener">${esc(it.page)}</a> · ${it.data.chars} 字 · ${it.data.headings.length} 个标题</div></div>
      <button class="btn btn-sm" data-act="copy" data-id="${it.id}">复制</button>
      <button class="btn btn-sm" data-act="txt" data-id="${it.id}">存为 TXT</button>
    </div>
    ${it.data.description ? `<p class="desc">${esc(it.data.description)}</p>` : ''}
    <pre>${esc(it.data.text)}</pre>
  </article>`;
const tableDoc = (it) => {
  const rows = it.data.rows;
  return `
  <article class="doc">
    <div class="doc-head">
      <div style="flex:1;min-width:0"><h3>${esc(it.note)}</h3>
      <div class="meta ellip">${rows.length} 行 · <a href="${esc(it.page)}" target="_blank" rel="noreferrer noopener">${esc(it.page)}</a></div></div>
      <button class="btn btn-sm" data-act="csv" data-id="${it.id}">导出 CSV</button>
    </div>
    <div class="tbl-wrap"><table class="tbl">${rows.slice(0, 60).map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</table></div>
    ${rows.length > 60 ? `<div class="meta" style="margin-top:8px">只预览前 60 行，导出的文件里是全部 ${rows.length} 行</div>` : ''}
  </article>`;
};

function toggle(id, el) {
  state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
  const on = state.selected.has(id);
  el.classList.toggle('sel', on);
  el.setAttribute?.('aria-checked', on);
  const cb = el.querySelector('input[type=checkbox]');
  if (cb) cb.checked = on;
  updateSel();
}
$('#view').addEventListener('click', (e) => {
  const act = e.target.dataset.act;
  if (act) {
    const it = state.items[+e.target.dataset.id];
    if (act === 'copy') navigator.clipboard.writeText(it.data.text).then(() => toast('已复制正文', 'success'), () => toast('复制失败', 'error'));
    if (act === 'txt') save(`${it.data.title || '正文'}.txt`, `${it.data.title}\n${it.page}\n\n${it.data.text}`, 'text/plain');
    if (act === 'csv') save(`${it.note || '表格'}.csv`, toCsv(it.data.rows), 'text/csv');
    return;
  }
  if (e.target.closest('a')) return;
  const el = e.target.closest('.tile, .row:not(.norow)');
  if (el) toggle(+el.dataset.id, el);
});
$('#view').addEventListener('keydown', (e) => {
  if ((e.key === ' ' || e.key === 'Enter') && e.target.classList.contains('tile')) { e.preventDefault(); toggle(+e.target.dataset.id, e.target); }
});
// 图片加载后记下宽度；太小的图标类图片可以用筛选去掉
$('#view').addEventListener('load', (e) => {
  if (e.target.tagName !== 'IMG') return;
  const t = e.target.closest('.tile');
  if (!t) return;
  state.dims[t.dataset.id] = e.target.naturalWidth;
  t.querySelector('.dim').textContent = `${e.target.naturalWidth}×${e.target.naturalHeight}`;
}, true);
$('#view').addEventListener('error', (e) => {
  if (e.target.tagName === 'IMG') e.target.closest('.tile')?.classList.add('broken');
}, true);

function updateSel() {
  const inTab = currentItems().filter((it) => state.selected.has(it.id)).length;
  const total = currentItems().length;
  $('#selInfo').textContent = DOWNLOADABLE.has(state.tab) ? (inTab ? `已选 ${inTab} / ${total}` : `共 ${total} 项`) : `共 ${total} 项`;
  $('#dlBtn').disabled = !inTab;
  $('#dlBtn').textContent = inTab ? `下载所选（${inTab}）` : '下载所选';
  $('#selectAll').textContent = total && inTab === total ? '取消全选' : '全选';
}
$('#selectAll').addEventListener('click', () => {
  const list = currentItems();
  const all = list.every((it) => state.selected.has(it.id));
  list.forEach((it) => (all ? state.selected.delete(it.id) : state.selected.add(it.id)));
  renderView();
});
let ft;
$('#filter').addEventListener('input', () => { clearTimeout(ft); ft = setTimeout(() => { state.shown = PAGE_SIZE; renderView(); }, 150); });
$('#minSize').addEventListener('change', renderView);
$('#moreBtn').addEventListener('click', () => { state.shown += PAGE_SIZE; renderView(); });

/* ───────── 导出 ───────── */
function save(name, text, mime) {
  const blob = new Blob([mime === 'text/csv' ? '﻿' : '', text], { type: mime + ';charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name.replace(/[\\/:*?"<>|]/g, '_').slice(0, 100);
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
const toCsv = (rows) => rows.map((r) => r.map((c) => /[",\n\r]/.test(c ?? '') ? `"${String(c).replace(/"/g, '""')}"` : (c ?? '')).join(',')).join('\r\n');
const baseName = () => `${host(state.summary?.params.url || '') || '结果'}_${LABEL[state.tab]}`;
$('#exportJson').addEventListener('click', () => {
  const list = currentItems();
  if (!list.length) return toast('没有可导出的内容');
  save(baseName() + '.json', JSON.stringify(list.map(({ id, ...rest }) => rest), null, 2), 'application/json');
});
$('#exportCsv').addEventListener('click', () => {
  const list = currentItems();
  if (!list.length) return toast('没有可导出的内容');
  let rows;
  if (state.tab === 'table') rows = list.flatMap((it, i) => [...(i ? [[]] : []), [`# ${it.note}`], ...it.data.rows]);
  else if (state.tab === 'custom') rows = [['文字', '链接', '标签', '来源页面'], ...list.map((it) => [it.data.text, it.data.link, it.data.tag, it.page])];
  else rows = [['类型', '名称', '说明', '网址', '来源页面'], ...list.map((it) => [LABEL[it.type], it.name, it.note, it.url, it.page])];
  save(baseName() + '.csv', toCsv(rows), 'text/csv');
});

/* ───────── 下载 ───────── */
let dlTimer = null;
$('#dlBtn').addEventListener('click', async () => {
  const items = currentItems().filter((it) => state.selected.has(it.id))
    .map(({ url, type, page, kind }) => ({ url, type, page, kind }));
  if (!items.length) return;
  try {
    await api('/api/download', { items });
    toast(`已加入下载：${items.length} 项`, 'success');
    $('#dlPanel').hidden = false;
    $('#dlPanel').classList.remove('folded');
    pollDl();
  } catch (err) { toast(err.message, 'error'); }
});
async function pollDl() {
  clearTimeout(dlTimer);
  let d;
  try { d = await api('/api/downloads'); } catch { return; }
  const tasks = d.tasks.slice().reverse();
  const active = tasks.filter((t) => t.status === 'queued' || t.status === 'running').length;
  const ok = tasks.filter((t) => t.status === 'done').length;
  const bad = tasks.filter((t) => t.status === 'error').length;
  $('#dlPanel').hidden = !tasks.length;
  $('#dlSummary').textContent = `${ok} 完成${active ? ` · ${active} 进行中` : ''}${bad ? ` · ${bad} 失败` : ''}`;
  $('#dlCancel').hidden = !active;
  const stateText = { queued: '排队中', canceled: '已取消', error: '失败' };
  $('#dlList').innerHTML = tasks.slice(0, 200).map((t) => `
    <div class="dl-item" ${t.path ? `data-path="${esc(t.path)}" title="点击在文件夹中显示" style="cursor:pointer"` : ''}>
      <div class="dl-top"><span class="ellip">${esc(t.name)}</span>
        <span class="dl-state ${t.status === 'done' ? 'ok' : t.status === 'error' ? 'bad' : ''}">${
          t.status === 'done' ? `完成 ${fmtSize(t.size)}` : t.status === 'running' ? `${t.percent ? t.percent + '%' : fmtSize(t.size) || '连接中'}` : stateText[t.status]}</span></div>
      ${t.status === 'running' ? `<div class="bar"><i style="width:${t.percent || 3}%"></i></div>` : ''}
      ${t.error ? `<div class="dl-err">${esc(t.error)}</div>` : ''}
    </div>`).join('');
  if (active) dlTimer = setTimeout(pollDl, 800);
}
$('#dlList').addEventListener('click', (e) => {
  const p = e.target.closest('[data-path]');
  if (p) api('/api/open', { path: p.dataset.path }).catch((err) => toast(err.message, 'error'));
});
$('#dlFold').addEventListener('click', () => $('#dlPanel').classList.toggle('folded'));
$('#dlCancel').addEventListener('click', () => api('/api/downloads/cancel', {}).then(pollDl));
$('#dlClear').addEventListener('click', () => api('/api/downloads/clear', {}).then(pollDl));
$('#openFolder').addEventListener('click', () => api('/api/open', {}).catch((err) => toast(err.message, 'error')));

/* ───────── 启动 ───────── */
renderChips();
paintTheme();
api('/api/info').then((i) => {
  state.info = i;
  if (!i.render) $('#renderWrap').hidden = true;
}).catch(() => {});
pollDl();
$('#url').focus();
