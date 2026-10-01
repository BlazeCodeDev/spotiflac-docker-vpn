/* SpotiFLAC — Material 3 Expressive UI preview.
 * Everything runs on dummy data from mock.js; no requests go to the server.
 * Views are plain template strings re-rendered into #view; clicks are handled by one
 * delegated listener that looks up data-act in the A table below. */
'use strict';

// ── Utilities ────────────────────────────────────────────────────────────
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ic = (n, c = '', style = '') => `<span class="ms ${c}" aria-hidden="true"${style ? ` style="${style}"` : ''}>${n}</span>`;
const clone = (o) => JSON.parse(JSON.stringify(o));
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const plural = (n, w, pl = w + 's') => `${n} ${n === 1 ? w : pl}`;
const fmtLen = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
const fmtMB = (mb) => (mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : mb.toFixed(1) + ' MB');
const getPath = (o, p) => p.split('.').reduce((a, k) => (a == null ? a : a[k]), o);
const setPath = (o, p, v) => { const ks = p.split('.'); const last = ks.pop(); ks.reduce((a, k) => a[k], o)[last] = v; };

const store = {
  get(k, d) { try { const v = localStorage.getItem('next.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('next.' + k, JSON.stringify(v)); } catch { /* private mode */ } },
  del(k) { try { localStorage.removeItem('next.' + k); } catch { /* ignore */ } },
};

// ── Theme ────────────────────────────────────────────────────────────────
// Every scheme is generated from a seed colour with Google's material-color-utilities (mcu.js,
// loaded on demand). "Device" uses the OS accent where the browser exposes it (CSS AccentColor).
// With no choice saved, the app follows the device colour if available, otherwise Neutral.
const SCHEMES = [
  { id: 'neutral', name: 'Neutral', seed: '#6C7278', neutral: true },
  { id: 'teal', name: 'Teal', seed: '#006A60' },
  { id: 'blue', name: 'Blue', seed: '#2F6BD7' },
  { id: 'indigo', name: 'Indigo', seed: '#4355B9' },
  { id: 'violet', name: 'Violet', seed: '#6750A4' },
  { id: 'pink', name: 'Rose', seed: '#984061' },
  { id: 'red', name: 'Red', seed: '#B3261E' },
  { id: 'orange', name: 'Orange', seed: '#C25E00' },
  { id: 'amber', name: 'Amber', seed: '#8B5000' },
  { id: 'green', name: 'Green', seed: '#1A6B26' },
];
const STATIC_SCHEMES = new Set(['neutral', 'teal', 'violet', 'amber']); // hand-written CSS used until the generator loads
const GENERIC_ACCENTS = new Set(['#000000', '#ffffff', '#0000ff', '#0055ff', '#0075ff', '#0060df']); // browser defaults, not a real device colour

function readDeviceAccent() {
  try {
    const forced = new URLSearchParams(location.search).get('accent'); // ?accent=%23ff0066 to try the device path
    if (forced && /^#[0-9a-f]{6}$/i.test(forced)) return forced.toLowerCase();
    if (!CSS.supports('color', 'AccentColor')) return null;
    const el = document.createElement('div');
    el.style.cssText = 'position:absolute;left:-9999px;background:AccentColor';
    document.body.appendChild(el);
    const rgb = getComputedStyle(el).backgroundColor;
    el.remove();
    const m = rgb.match(/rgb\((\d+),\s*(\d+),\s*(\d+)/);
    if (!m) return null;
    const hex = '#' + [m[1], m[2], m[3]].map((x) => Number(x).toString(16).padStart(2, '0')).join('');
    return GENERIC_ACCENTS.has(hex) ? null : hex;
  } catch { return null; }
}
const DEVICE = readDeviceAccent();

const schemeId = () => {
  const s = store.get('scheme', null);
  if (s === 'device' && DEVICE) return 'device';
  if (s === 'custom') return 'custom';
  if (SCHEMES.some((x) => x.id === s)) return s;
  return DEVICE ? 'device' : 'neutral';
};
const schemeSeed = (id) => (id === 'device' ? DEVICE : id === 'custom' ? store.get('seed', '#6750A4') : SCHEMES.find((x) => x.id === id).seed);

let MCU = null;
const varsCache = new Map();
const ROLES = { primary: 'primary', 'on-primary': 'onPrimary', 'primary-container': 'primaryContainer', 'on-primary-container': 'onPrimaryContainer',
  'secondary-container': 'secondaryContainer', 'on-secondary-container': 'onSecondaryContainer', tertiary: 'tertiary', 'tertiary-container': 'tertiaryContainer',
  'on-tertiary-container': 'onTertiaryContainer', error: 'error', 'on-error': 'onError', 'error-container': 'errorContainer', 'on-error-container': 'onErrorContainer',
  surface: 'surface', 'on-surface': 'onSurface', 'on-surface-variant': 'onSurfaceVariant', outline: 'outline', 'outline-variant': 'outlineVariant',
  'sc-lowest': 'surfaceContainerLowest', 'sc-low': 'surfaceContainerLow', sc: 'surfaceContainer', 'sc-high': 'surfaceContainerHigh', 'sc-highest': 'surfaceContainerHighest',
  'inverse-surface': 'inverseSurface', 'inverse-on-surface': 'inverseOnSurface', 'inverse-primary': 'inversePrimary' };
function schemeVars(seed, neutral) {
  if (!MCU || !seed) return null;
  const key = seed + (neutral ? ':n' : '');
  if (!varsCache.has(key)) {
    const out = {};
    for (const mode of ['light', 'dark']) {
      const Scheme = neutral ? MCU.SchemeNeutral : MCU.SchemeTonalSpot;
      const sch = new Scheme(MCU.Hct.fromInt(MCU.argbFromHex(seed)), mode === 'dark', 0);
      out[mode] = {};
      for (const [k, r] of Object.entries(ROLES)) out[mode][k] = MCU.hexFromArgb(MCU.MaterialDynamicColors[r].getArgb(sch));
    }
    varsCache.set(key, out);
  }
  return varsCache.get(key);
}
function applyTheme() {
  const root = document.documentElement;
  let mode = store.get('mode', 'auto');
  if (mode === 'auto') mode = matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  root.dataset.mode = mode;
  const id = schemeId();
  root.dataset.scheme = STATIC_SCHEMES.has(id) ? id : 'neutral';
  const v = schemeVars(schemeSeed(id), id === 'neutral');
  if (v) {
    for (const [k, val] of Object.entries(v[mode])) root.style.setProperty('--md-' + k, val);
    store.set('themevars', v); // lets the next page load paint in these colours before any script runs
  }
  if ($('#lib-aside')) tintDetail();
  const meta = $('meta[name=theme-color]');
  if (meta) meta.content = getComputedStyle(root).getPropertyValue('--md-sc').trim() || '#0E1513';
}

// ── API ──────────────────────────────────────────────────────────────────
async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method, credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (!res.ok) {
    const e = new Error((data && (data.error || data.message)) || `${res.status} ${res.statusText}`);
    e.status = res.status; e.data = data;
    throw e;
  }
  return data;
}
const oops = (what) => (e) => snack(`${what}: ${e.message}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const parseTime = (s) => (s ? new Date(s).getTime() : 0);
function ago(ms) {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
function span(sec) {
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h >= 24 ? `${Math.floor(h / 24)} d ${h % 24} h` : h ? `${h} h ${m} min` : `${m} min`;
}

// ── State ────────────────────────────────────────────────────────────────
const ALL_SERVICES = [['tidal', 'Tidal'], ['qobuz', 'Qobuz'], ['amazon', 'Amazon Music'], ['deezer', 'Deezer'], ['youtube', 'YouTube']];
const serviceName = (id) => (ALL_SERVICES.find((s) => s[0] === id) || [id, id])[1];
// UI-shaped settings; fromServer()/toServer() translate to and from /api/settings.
const DEFAULT_SET = {
  parallel: 3, delay: 4, retryEvery: 5, maxRetries: 3, timeout: 300, reconnect: 3,
  fmt: '{artist}/{album}/{track} {title}', m3u: 'ask',
  sources: ALL_SERVICES.map(([id, name]) => ({ id, name, on: false })), qobuz: '', registry: '',
  meta: { on: true, deezer: true, apple: true, qobuz: false, tidal: false, mb: true },
  lb: { on: false, user: '', days: [0, 1, 2, 3, 4, 5, 6], time: '06:00' },
};
function fromServer(c) {
  const on = c.services || [];
  const order = [...on, ...ALL_SERVICES.map((s) => s[0]).filter((id) => !on.includes(id))];
  const prov = c.enrich_providers || [];
  return {
    parallel: c.max_workers, delay: c.track_delay_s, retryEvery: c.retry_interval_min, maxRetries: c.retry_max_count,
    timeout: c.download_timeout_s, reconnect: c.reconnect_threshold, fmt: c.filename_fmt, m3u: c.m3u_mode,
    sources: order.map((id) => ({ id, name: serviceName(id), on: on.includes(id) })),
    qobuz: c.qobuz_token || '', registry: (c.extension_registries || []).join('\n'),
    meta: { on: !!c.enrich_metadata, deezer: prov.includes('deezer'), apple: prov.includes('apple'), qobuz: prov.includes('qobuz'), tidal: prov.includes('tidal'), mb: !!c.enrich_musicbrainz },
    lb: { on: !!c.listenbrainz_enabled, user: c.listenbrainz_username || '', days: c.listenbrainz_days || [], time: c.listenbrainz_time || '06:00' },
  };
}
function toServer() {
  const s = S.set;
  return {
    max_workers: s.parallel, track_delay_s: s.delay, retry_interval_min: s.retryEvery, retry_max_count: s.maxRetries,
    download_timeout_s: s.timeout, reconnect_threshold: s.reconnect, filename_fmt: s.fmt, m3u_mode: s.m3u,
    services: s.sources.filter((x) => x.on).map((x) => x.id), qobuz_token: s.qobuz,
    extension_registries: s.registry.split(/[\s,]+/).filter(Boolean),
    enrich_metadata: s.meta.on, enrich_providers: ['deezer', 'apple', 'tidal', 'qobuz'].filter((k) => s.meta[k]), enrich_musicbrainz: s.meta.mb,
    listenbrainz_enabled: s.lb.on, listenbrainz_username: s.lb.user, listenbrainz_days: s.lb.days, listenbrainz_time: s.lb.time,
    quality: S.quality,
  };
}
const S = {
  route: 'download',
  // download
  q: '', type: 'all', results: [], resultsFor: '', hasMore: false, nextOffset: 0, searching: false, searchError: '', inLib: {},
  quality: store.get('quality', 'lossless'),
  jobs: [], jobsLoaded: false, doneAll: false,
  vpn: { known: false, on: false, since: null, ip: null },
  tasks: [],
  // library
  lib: { filtersOpen: false, path: [], view: 'tracks', chips: new Set(), q: '', sel: new Set(), focus: null, tracks: [], loaded: false, ready: true, pending: 0, error: '' },
  // health: null = no scan result yet
  h: {
    tab: 'dups', filters: new Set(['id', 'tags']),
    dups: null, dupsInfo: null, keep: {}, mis: null, misInfo: null,
    sel: { mis: new Set(), name: new Set(), miss: new Set() },
  },
  section: 'appearance',
  set: clone(DEFAULT_SET), setLoaded: false, setError: '',
  ext: null, providers: [], ver: null,
};

// ── Derived data ─────────────────────────────────────────────────────────
const missingOf = (t) => t._miss || (t._miss = ['genre', 'mbid', 'bpm', 'cover'].filter((k) => !t[k]));
const expectedFile = (t) => t.expected || t.file;
const trackPath = (t) => t.path;
const misnamed = () => S.lib.tracks.filter((t) => t.expected && t.file !== t.expected);
const missingList = () => S.lib.tracks.filter((t) => missingOf(t).length);
const dupGroups = () => S.h.dups || [];
const dupFor = (t) => dupGroups().find((g) => g.files.some((f) => f.path === t.path));
const queueJobs = () => {
  const rank = { running: 0, queued: 1, error: 2, cancelled: 3 };
  return S.jobs.filter((j) => j.status in rank).slice().reverse().sort((a, b) => rank[a.status] - rank[b.status]);
};
const doneJobs = () => S.jobs.filter((j) => j.status === 'done').sort((a, b) => parseTime(b.finished_at) - parseTime(a.finished_at));
const activeJobs = () => S.jobs.filter((j) => j.status === 'queued' || j.status === 'running').length;
const jobKind = (j) => ((j.url || '').match(/spotify\.com\/(track|album|playlist|artist)\//) || [])[1] || 'track';
const jobTitle = (j) => j.title || j.url || 'Download';

// ── Snackbar + dialog ────────────────────────────────────────────────────
let snackTimer, snackAction;
function snack(msg, action) {
  const el = $('#snack');
  snackAction = action && action.fn;
  el.innerHTML = `<span class="b-m">${esc(msg)}</span>${action ? `<button class="btn text" data-snack-act>${esc(action.label)}</button>` : ''}`;
  el.classList.add('show');
  clearTimeout(snackTimer);
  snackTimer = setTimeout(() => el.classList.remove('show'), action ? 6000 : 3500);
}
let savedTimer;
function saved() {
  clearTimeout(savedTimer);
  savedTimer = setTimeout(async () => {
    try {
      await api('/api/settings', { method: 'PATCH', body: toServer() });
      snack('Saved');
    } catch (e) {
      const f = e.data && e.data.fields;
      snack(f ? `Not saved: ${Object.entries(f).map(([k, v]) => `${k} ${v}`).join(', ')}` : `Not saved: ${e.message}`);
    }
  }, 600);
}

function dialog(title, body, actions) {
  return new Promise((resolve) => {
    const d = $('#dlg');
    d.innerHTML = `<h2 class="t-l" style="margin-bottom:16px">${esc(title)}</h2><div class="b-m v">${body}</div>
      <div class="actions">${actions.map((a) => `<button class="btn ${a.cls || 'text'}" data-dlg="${a.key}">${esc(a.label)}</button>`).join('')}</div>`;
    const finish = (v) => { d.onclick = null; d.oncancel = null; if (d.open) d.close(); resolve(v); };
    d.onclick = (e) => {
      const b = e.target.closest('[data-dlg]');
      if (b) return finish(b.dataset.dlg);
      const r = d.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) finish(null);
    };
    d.oncancel = () => finish(null);
    if (!d.open) d.showModal();
  });
}
const confirmDlg = (title, body, yes, danger = true) =>
  dialog(title, body, [{ key: 'no', label: 'Cancel' }, { key: 'yes', label: yes, cls: danger ? 'text danger' : 'filled' }]).then((k) => k === 'yes');

const APP = window.APP || { commit: '', assets: '/static/app/' };
const brandHTML = () => `<a href="#/download" class="brand" aria-label="SpotiFLAC, home"><img src="${APP.assets}logo.svg" alt="" width="36" height="36"><span class="brand-name">Spoti<b>FLAC</b></span></a>
  ${APP.commit ? `<span class="commit mono" title="Build commit">${esc(APP.commit)}</span>` : ''}`;
const vpnPill = () => {
  const v = S.vpn;
  const cls = !v.known ? 'unk' : v.on ? 'on' : 'off';
  const txt = !v.known ? 'Checking…' : v.on ? 'Connected' : 'Not connected';
  return `<button class="vpn ${cls}" data-act="vpn" aria-label="VPN ${v.known ? txt.toLowerCase() : 'status loading'}. Show details">
  ${ic(v.on || !v.known ? 'vpn_lock' : 'vpn_key_off', 'f')}<span class="l-m">VPN</span><span class="vpn-s">${txt}</span></button>`;
};

// ── Rail ─────────────────────────────────────────────────────────────────
const NAV = [
  ['download', 'Download', 'download'],
  ['library', 'Library', 'library_music'],
  ['health', 'Health', 'health_and_safety'],
  ['settings', 'Settings', 'settings'],
];
function railHTML() {
  const badge = { download: activeJobs() };
  const link = (id, label, icon, cls = '') => {
    const n = badge[id] || 0;
    return `<a href="#/${id}" class="rail-item ${S.route === id ? 'on' : ''} ${cls}" ${S.route === id ? 'aria-current="page"' : ''}>
      <span class="ind">${ic(icon)}${n ? `<span class="badge" aria-label="${n} items">${n}</span>` : ''}</span><span>${label}</span></a>`;
  };
  // Settings sits with the status controls at the bottom of the desktop rail, but stays in the phone's bottom bar.
  const items = NAV.map(([id, label, icon]) => link(id, label, icon, id === 'settings' ? 'mobile-only' : '')).join('');
  const settings = NAV.filter((n) => n[0] === 'settings').map(([id, label, icon]) => link(id, label, icon, 'rail-extra')).join('');
  const dark = document.documentElement.dataset.mode === 'dark';
  return `<div class="col rail-extra" style="align-items:center;gap:8px;margin-bottom:12px">${brandHTML()}</div>
    <button class="fab" data-act="paste" aria-label="Paste link and download" style="margin:4px 0 20px">${ic('add_link')}</button>
    ${items}
    <div class="grow rail-extra"></div>
    <button class="ib rail-extra" data-act="theme" aria-label="Switch to ${dark ? 'light' : 'dark'} mode">${ic(dark ? 'light_mode' : 'dark_mode')}</button>
    ${settings}
    <span class="rail-extra">${vpnPill()}</span>
    ${S.route === 'download' || S.route === 'health' || (S.route === 'library' && S.lib.sel.size) ? '' : `<button class="fab ext mfab" data-act="paste">${ic('add_link')}<span class="l-l">Paste link</span></button>`}`;
}

// ── Shared bits ──────────────────────────────────────────────────────────
const emptyState = (icon, title, hint = '') =>
  `<div class="empty">${ic(icon)}<div class="t-m" style="color:var(--md-on-surface)">${esc(title)}</div>${hint ? `<div class="b-m">${esc(hint)}</div>` : ''}</div>`;

// The first running background task (library index, enrichment, duplicate/mistag scan, ListenBrainz sync).
function busyTask() {
  const t = S.tasks.find((x) => x.running);
  if (!t) return null;
  const pct = t.progress_total ? Math.min(100, (t.progress_done / t.progress_total) * 100) : null;
  return { label: t.label, pct, text: pct == null ? `${t.label}…` : `${t.label} · ${Math.round(pct)}%` };
}
const runningTasks = () => S.tasks.filter((x) => x.running);
// The running library scan specifically (the corner chip shows whichever task is first).
function scanInfo() {
  const t = S.tasks.find((x) => x.running && x.id.startsWith('scan-'));
  if (!t) return null;
  const pct = t.progress_total ? Math.min(100, (t.progress_done / t.progress_total) * 100) : null;
  return { pct, text: t.detail || 'Scanning…' };
}
function scanChip() {
  const b = busyTask();
  if (!b) return '';
  const n = runningTasks().length;
  const arc = ((b.pct == null ? 25 : b.pct) / 100) * 47.12;
  return `<button class="chip" data-act="tasks" aria-haspopup="dialog" aria-label="Background tasks: ${esc(b.text)}${n > 1 ? `, and ${n - 1} more` : ''}. Show details">
    <svg width="18" height="18" viewBox="0 0 20 20" aria-hidden="true" ${b.pct == null ? 'class="spin"' : ''}><circle cx="10" cy="10" r="7.5" fill="none" stroke="var(--md-secondary-container)" stroke-width="3"/>
    <circle data-scan-arc cx="10" cy="10" r="7.5" fill="none" stroke="var(--md-primary)" stroke-width="3" stroke-linecap="round" stroke-dasharray="${arc} 47.12" transform="rotate(-90 10 10)"/></svg>
    <span data-scan-text>${esc(b.text)}</span>${n > 1 ? `<span class="tag primary" style="height:20px;padding:0 8px">+${n - 1}</span>` : ''}</button>`;
}
function patchScan() {
  const sc = scanInfo();
  if (sc) {
    $$('[data-scan-health]').forEach((e) => { e.textContent = sc.text; });
    $$('[data-scan-bar] .a').forEach((e) => { e.style.width = (sc.pct == null ? 30 : sc.pct) + '%'; });
  }
  const b = busyTask();
  if (!b) return;
  $$('[data-scan-text]').forEach((e) => { e.textContent = b.text; });
  $$('[data-scan-arc]').forEach((e) => e.setAttribute('stroke-dasharray', `${((b.pct == null ? 25 : b.pct) / 100) * 47.12} 47.12`));
}

// ═════════════════════════════════════════════════════════════════════════
// Download
// ═════════════════════════════════════════════════════════════════════════
const TYPE_ICON = { track: 'music_note', album: 'album', playlist: 'queue_music', artist: 'person' };
const LINK_RE = /open\.spotify\.com\/(track|album|playlist|artist)\/([A-Za-z0-9]+)/;

// A cover slot: the icon (and a colour from the title) shows until the image loads, or if it fails.
function coverGradient(seed) {
  let h = 0; for (const ch of String(seed)) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `linear-gradient(135deg,hsl(${h} 45% 40%),hsl(${(h + 45) % 360} 50% 26%))`;
}
const coverImg = (url) => (url ? `<img src="${esc(url)}" alt="" loading="lazy" onerror="this.remove()">` : '');
function coverTile(d, size = 48) {
  return `<span class="coverthumb" style="width:${size}px;height:${size}px;background:${coverGradient(d.title)}" aria-hidden="true">${ic(TYPE_ICON[d.kind] || 'album', '', `font-size:${Math.round(size / 2.2)}px`)}${coverImg(d.url)}</span>`;
}
// Library rows always show a cover. Tracks in one folder share art, so one request per folder serves them all.
const dirCover = new Map();
function rebuildDirCovers() {
  dirCover.clear();
  for (const t of S.lib.tracks) if (t.cover && !dirCover.has(t.dir)) dirCover.set(t.dir, t.path);
}
const coverUrl = (t) => (t.cover ? `/api/library/cover?path=${encodeURIComponent(dirCover.get(t.dir) || t.path)}` : '');
const libCover = (t) => (t.cover ? coverTile({ title: t.album || t.title, kind: 'track', url: coverUrl(t) }, 40)
  : `<span class="coverthumb" style="width:40px;height:40px;background:var(--md-sc-highest);color:var(--md-outline)" role="img" aria-label="No cover art">${ic('image_not_supported', '', 'font-size:20px')}</span>`);

// ── Search ───────────────────────────────────────────────────────────────
let searchTimer, searchSeq = 0;
function scheduleSearch() {
  clearTimeout(searchTimer);
  const q = S.q.trim();
  if (!q || LINK_RE.test(q)) { searchSeq++; S.results = []; S.resultsFor = ''; S.searching = false; S.searchError = ''; return; }
  S.searching = true; S.searchError = '';
  searchTimer = setTimeout(() => runSearch(q, 0), 450);
}
async function runSearch(q, offset) {
  const seq = ++searchSeq;
  S.searching = true; S.searchError = '';
  if (offset) renderResults();
  try {
    const d = await api(`/api/search?q=${encodeURIComponent(q)}&offset=${offset}`);
    if (seq !== searchSeq) return;
    S.results = offset ? S.results.concat(d.results || []) : (d.results || []);
    S.resultsFor = q; S.hasMore = !!d.has_more; S.nextOffset = d.next_offset || 0; S.searching = false;
    renderResults();
    checkLibrary(d.results || []);
  } catch (e) {
    if (seq !== searchSeq) return;
    S.searching = false; S.searchError = e.message; renderResults();
  }
}
async function checkLibrary(items) {
  const list = items.filter((r) => r.type === 'track' || r.type === 'album' || r.type === 'playlist')
    .map((r) => ({ type: r.type, url: r.url, title: r.title, track_count: r.track_count }));
  if (!list.length) return;
  try { Object.assign(S.inLib, await api('/api/library/check-items', { method: 'POST', body: { items: list } })); renderResults(); } catch { /* the badge just doesn't show */ }
}

function resultsHTML() {
  const q = S.q.trim();
  if (!q) return emptyState('search', 'Search Spotify or paste a link', 'Tracks, albums and playlists download straight from a link.');
  const link = q.match(LINK_RE);
  if (link) {
    return `<div class="li"><div class="lead">${ic('link')}</div><div class="grow col"><span class="b-l">Spotify ${link[1]} link</span><span class="b-m v ell">${esc(q)}</span></div>
      <button class="btn filled" data-act="dllink" data-type="${link[1]}">${ic('download')}Download</button></div>`;
  }
  if (S.searching && !S.results.length) return Array.from({ length: 4 }, () => `<div class="li"><div class="lead skel"></div><div class="grow col" style="gap:8px"><span class="skel" style="height:14px;width:55%;border-radius:4px"></span><span class="skel" style="height:12px;width:35%;border-radius:4px"></span></div></div>`).join('');
  if (S.searchError) return emptyState('cloud_off', 'Search failed', S.searchError);
  const shown = S.results.map((r, i) => [r, i]).filter(([r]) => S.type === 'all' || r.type === S.type);
  if (!shown.length) return emptyState('search_off', 'No matches', `Nothing for “${S.q}”. Try fewer words or paste a Spotify link.`);
  return shown.map(([r, i]) => {
    const st = S.inLib[r.url];
    return `<div class="li">
      <div class="lead">${ic(TYPE_ICON[r.type])}${coverImg(r.cover_url)}</div>
      <div class="grow col"><span class="b-l ell">${esc(r.title)}</span><span class="b-m v ell">${cap(r.type)}${r.subtitle ? ' · ' + esc(r.subtitle) : ''}${r.year ? ' · ' + r.year : ''}${r.track_count ? ' · ' + plural(r.track_count, 'track') : ''}</span></div>
      ${st === 'full' ? `<span class="tag primary hide-sm">${ic('check')}In library</span>` : st === 'partial' ? `<span class="tag neutral hide-sm">Partly in library</span>` : ''}
      <span class="mono l-m v hide-sm" style="width:36px;text-align:right">${r.duration_ms ? fmtLen(Math.round(r.duration_ms / 1000)) : ''}</span>
      <button class="ib ${st === 'full' ? 'outlined' : 'filled'}" data-act="dl" data-i="${i}" aria-label="Download ${esc(r.title)}">${ic('download')}</button></div>`;
  }).join('') + (S.hasMore ? `<div class="row" style="justify-content:center;padding:8px"><button class="btn tonal" data-act="more" ${S.searching ? 'disabled' : ''}>${S.searching ? 'Loading…' : 'Load more'}</button></div>` : '');
}

// ── Queue ────────────────────────────────────────────────────────────────
const jobPct = (j) => (j.status === 'running' && j.total ? Math.min(100, ((j.progress || 0) / j.total) * 100) : null);
const jobNow = (j) => ((j.track_results || []).find((t) => t.status === 'downloading') || {}).title;
function jobNote(j) {
  const total = j.total || 0, prog = j.progress || 0, cur = jobNow(j);
  if (j.status === 'running') return total > 1 ? `${cur ? cur + ' · ' : ''}${prog} of ${total} tracks` : (cur || 'Downloading');
  if (j.status === 'queued') return 'Waiting for a free worker';
  if (j.status === 'cancelled') return 'Cancelled';
  const mins = j.next_retry_at ? Math.max(0, Math.round((parseTime(j.next_retry_at) - Date.now()) / 60000)) : null;
  return (j.error || 'Failed') + (mins != null && !Number.isNaN(mins) ? ` · retrying in ${mins} min` : '');
}
function jobCard(j) {
  const st = j.status, pct = jobPct(j);
  const meta = st === 'running' ? (pct != null ? `${Math.round(pct)}%` : '') : st === 'queued' ? (j.total ? plural(j.total, 'track') : '')
    : st === 'error' && j.retry_count ? `try ${j.retry_count}${j.retry_max ? '/' + j.retry_max : ''}` : '';
  const icon = st === 'error' ? 'error' : TYPE_ICON[jobKind(j)] || 'music_note';
  const btn = (act, icn, label) => `<button class="ib" data-act="${act}" data-id="${j.id}" aria-label="${label} ${esc(jobTitle(j))}" style="color:inherit">${ic(icn)}</button>`;
  const actions = st === 'error' || st === 'cancelled' ? btn('jretry', 'refresh', 'Retry') + btn('jremove', 'close', 'Remove')
    : btn('jcancel', 'close', st === 'queued' ? 'Remove' : 'Cancel');
  return `<div class="card ${st === 'error' ? 'err' : st === 'running' ? '' : 'idle'}">
    <div class="row" style="gap:14px"><div class="lead" style="background:${st === 'error' ? 'var(--md-error)' : ''}">${ic(icon)}${st === 'error' ? '' : coverImg(j.cover_url)}</div>
      <div class="grow col"><span class="t-m ell">${esc(jobTitle(j))}</span><span class="b-m ell" style="opacity:.85" data-job-note="${j.id}">${esc(jobNote(j))}</span></div>
      <span class="mono l-m" style="opacity:.85" data-job-pct="${j.id}">${meta}</span>${actions}</div>
    ${st === 'running' ? `<div class="lp ${pct == null ? 'ind' : ''}" role="progressbar" aria-label="Progress" aria-valuemin="0" aria-valuemax="100" ${pct != null ? `aria-valuenow="${Math.round(pct)}"` : ''} data-job-bar="${j.id}"><span class="a" style="width:${pct == null ? 30 : pct}%"></span><span class="t"></span></div>` : ''}
  </div>`;
}

function finishedCard() {
  const all = doneJobs();
  const shown = S.doneAll ? all : all.slice(0, 5);
  const row = (j) => {
    const ok = j.success_count ?? j.total ?? 1, bad = j.fail_count || 0, kind = jobKind(j);
    return `<div class="li" style="min-height:64px;padding:6px 8px 6px 12px">
      ${coverTile({ title: jobTitle(j), kind, url: j.cover_url })}
      <div class="grow col"><span class="b-l ell">${esc(jobTitle(j))}</span><span class="b-m v ell">${cap(kind)}${ok > 1 ? ' · ' + plural(ok, 'track') : ''} · ${ago(parseTime(j.finished_at))}</span></div>
      ${bad ? `<span class="tag error hide-sm">${bad} failed</span><button class="ib" data-act="jretrypart" data-id="${j.id}" title="Retry the failed tracks" aria-label="Retry failed tracks of ${esc(jobTitle(j))}">${ic('refresh')}</button>` : ''}
      ${ok > 0 ? `<button class="ib tonal" data-act="dldone" data-id="${j.id}" title="Download to your browser${ok > 1 ? ' (.zip)' : ''}" aria-label="Download ${esc(jobTitle(j))} to your browser">${ic('download')}</button>` : ''}</div>`;
  };
  return `<section class="pane" aria-labelledby="h-done" style="padding:20px;display:flex;flex-direction:column;gap:8px">
    <div class="row"><h2 id="h-done" class="t-l grow">Finished</h2>${all.length ? `<span class="tag primary">${all.length} done</span><button class="btn text" data-act="cleardone">Clear</button>` : ''}</div>
    ${shown.length ? `<div class="col" style="gap:2px">${shown.map(row).join('')}</div>` : emptyState('download_done', 'Nothing finished yet', 'Completed downloads show up here.')}
    ${all.length > 5 ? `<button class="btn text" style="align-self:center" data-act="doneopen" aria-expanded="${S.doneAll}">${S.doneAll ? 'Show less' : `Show all ${all.length}`}</button>` : ''}
  </section>`;
}

function resPaneHTML() {
  const types = [['all', 'All'], ['track', 'Tracks'], ['album', 'Albums'], ['playlist', 'Playlists'], ['artist', 'Artists']];
  const count = S.results.filter((r) => S.type === 'all' || r.type === S.type).length;
  return `<div class="row" style="padding:0 12px;align-items:baseline"><h2 id="h-res" class="t-l">Results</h2><span class="b-m v">${S.q.trim() && !LINK_RE.test(S.q) && S.results.length ? plural(count, 'match', 'matches') : ''}</span></div>
      <div class="row wrap" style="padding:0 12px;gap:8px" role="group" aria-label="Result type">
        ${types.map(([k, l]) => `<button class="chip ${S.type === k ? 'on' : ''}" aria-pressed="${S.type === k}" data-act="type" data-k="${k}">${S.type === k ? ic('check') : ''}${l}</button>`).join('')}
      </div>
      <div class="col" style="gap:2px">${resultsHTML()}</div>`;
}
// Redraw only the Results card, so the search box keeps focus, caret and any half-typed (IME) text.
function renderResults() {
  const pane = $('#res-pane');
  if (S.route !== 'download' || !pane) return render();
  pane.classList.toggle('idle', !S.q.trim());
  pane.innerHTML = resPaneHTML();
  const clr = $('[data-act=clearq]'); if (clr) clr.hidden = !S.q;
}
function viewDownload() {
  return `
  <header class="row wrap" style="gap:16px">
    <label class="searchbar grow" style="max-width:760px">${ic('search', 'v')}
      <input id="q" type="search" enterkeyhint="search" value="${esc(S.q)}" placeholder="Paste Spotify links or search" aria-label="Paste Spotify links or search" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" data-input="q">
      <button class="ib" data-act="clearq" aria-label="Clear" ${S.q ? '' : 'hidden'}>${ic('close')}</button>
      <button class="ib" data-act="pasteq" aria-label="Paste from clipboard">${ic('content_paste')}</button></label>
    <div class="seg" role="radiogroup" aria-label="Quality">
      ${[['high', 'High'], ['lossless', 'Lossless'], ['hires', 'Hi-Res']].map(([k, l]) => `<button role="radio" aria-checked="${S.quality === k}" data-act="quality" data-k="${k}">${S.quality === k ? ic('check') : ''}${l}</button>`).join('')}
    </div>
  </header>
  <div class="split">
    <section class="pane res-pane ${S.q.trim() ? '' : 'idle'}" id="res-pane" aria-labelledby="h-res" style="padding:20px 12px;display:flex;flex-direction:column;gap:12px">${resPaneHTML()}</section>
    <div class="col" id="dl-side" style="gap:16px;min-width:0">${dlSideHTML()}</div>
  </div>`;
}
function dlSideHTML() {
  const n = (s) => S.jobs.filter((j) => j.status === s).length;
  const failed = n('error');
  const q = queueJobs();
  return `<section class="pane" aria-labelledby="h-q" style="padding:20px;display:flex;flex-direction:column;gap:12px">
      <div class="row"><h2 id="h-q" class="t-l grow">Queue</h2>
        ${failed ? `<button class="btn text" data-act="retryall">Retry failed</button>` : ''}
</div>
      <div class="row" style="gap:8px"><span class="tag tertiary">${n('running')} running</span><span class="tag neutral">${n('queued')} queued</span>${failed ? `<span class="tag error">${failed} failed</span>` : ''}</div>
      ${!S.jobsLoaded ? '<div class="card idle"><span class="skel" style="height:16px;width:60%;border-radius:4px"></span></div>' : q.length ? q.map(jobCard).join('') : emptyState('done_all', 'Queue is empty', 'Search for something to download.')}
    </section>
    ${finishedCard()}`;
}

// ── Jobs: polled from the server ─────────────────────────────────────────
let jobsSig = '', prevStatus = null;
async function refreshJobs() {
  let d;
  try { d = await api('/api/jobs'); } catch { return; }
  const sig = JSON.stringify(d.map((j) => [j.id, j.status, j.title, j.total, j.success_count, j.fail_count, j.error, j.next_retry_at, j.cover_url, j.retry_count, j.finished_at]));
  const first = !S.jobsLoaded;
  S.jobs = d; S.jobsLoaded = true;
  if (prevStatus) for (const j of d) { const was = prevStatus[j.id]; if (was && was !== j.status) { if (j.status === 'done') snack(`Done: ${jobTitle(j)}`); else if (j.status === 'error') snack(`Failed: ${jobTitle(j)}`); } }
  prevStatus = Object.fromEntries(d.map((j) => [j.id, j.status]));
  if (sig !== jobsSig || first) {
    jobsSig = sig;
    renderChrome();
    const side = $('#dl-side'); if (S.route === 'download' && side) side.innerHTML = dlSideHTML();
  } else patchJobs();
}
function patchJobs() {
  for (const j of S.jobs) {
    if (j.status !== 'running') continue;
    const pct = jobPct(j);
    const bar = $(`[data-job-bar="${j.id}"]`);
    if (bar && pct != null) { bar.firstElementChild.style.width = pct + '%'; bar.setAttribute('aria-valuenow', Math.round(pct)); }
    const p = $(`[data-job-pct="${j.id}"]`); if (p && pct != null) p.textContent = Math.round(pct) + '%';
    const nt = $(`[data-job-note="${j.id}"]`); if (nt) nt.textContent = jobNote(j);
  }
}
let tasksSig = '';
async function refreshTasks() {
  let d;
  try { d = await api('/api/tasks'); } catch { return; }
  const tasks = Array.isArray(d) ? d : d.tasks || [];
  const prev = S.tasks; S.tasks = tasks;
  const sig = JSON.stringify(tasks.map((t) => [t.id, t.running, t.label]));
  // A scan that just finished has a result to fetch; enrichment/index changes mean new tag data.
  for (const t of prev) {
    if (!t.running) continue;
    const now = tasks.find((x) => x.id === t.id);
    if (now && now.running) continue;
    if (t.id === 'scan-library') { loadScan('dups'); loadScan('mistag'); loadTracks(true); }
    else if (t.id === 'scan-dups') loadScan('dups');
    else if (t.id === 'scan-mistag') loadScan('mistag');
    else if (t.id === 'lib-index' || t.id === 'lib-enrich') loadTracks(true);
  }
  const tb = $('#tasks-body');
  if (tb) tb.innerHTML = tasksBody();
  if (sig !== tasksSig) { tasksSig = sig; renderChrome(); if (S.route === 'health') render(); } else patchScan();
}

async function enqueue(r) {
  let m3u = false;
  if (r.type === 'playlist' && S.set.m3u === 'ask') {
    const k = await dialog('Create an M3U playlist?', 'Also write a .m3u file for this playlist, in a Playlists folder.', [{ key: 'cancel', label: 'Cancel' }, { key: 'no', label: 'No' }, { key: 'yes', label: 'Yes', cls: 'filled' }]);
    if (!k || k === 'cancel') return;
    m3u = k === 'yes';
  }
  try {
    await api('/api/download', { method: 'POST', body: { urls: r.url, quality: S.quality, pre_title: r.title || '', generate_m3u: m3u } });
    snack(r.title ? `Added “${r.title}” to the queue` : 'Added to the queue');
    refreshJobs();
  } catch (e) { oops('Couldn’t queue it')(e); }
}

// ═════════════════════════════════════════════════════════════════════════
// Library
// ═════════════════════════════════════════════════════════════════════════
const CHIPS = [['lossless', 'Lossless only'], ['missing', 'Missing tags'], ['mbid', 'No MusicBrainz ID'], ['cover', 'No cover art'], ['new', 'Added this week']];
const TAG_ICONS = [['genre', 'sell', 'genre'], ['mbid', 'fingerprint', 'MusicBrainz ID'], ['bpm', 'speed', 'BPM'], ['cover', 'image', 'cover']];

function libScope() {
  const p = S.lib.path.join('/');
  return S.lib.tracks.filter((t) => !p || t.dir === p || t.dir.startsWith(p + '/'));
}
function libFiltered(list) {
  const { chips, q } = S.lib; const needle = q.trim().toLowerCase();
  return list.filter((t) =>
    (!chips.has('lossless') || t.lossless) && (!chips.has('missing') || missingOf(t).length) &&
    (!chips.has('mbid') || !t.mbid) && (!chips.has('cover') || !t.cover) && (!chips.has('new') || t.addedDays <= 7) &&
    (!needle || (t._hay || `${t.title} ${t.artist} ${t.album} ${t.year}`.toLowerCase()).includes(needle)));
}
function childFolders(p) {
  const prefix = p ? p + '/' : '';
  const m = new Map();
  for (const t of S.lib.tracks) {
    if (!t.dir.startsWith(prefix)) continue;
    const rest = t.dir.slice(prefix.length);
    if (!rest) continue;
    const name = rest.split('/')[0];
    if (!m.has(name)) m.set(name, { name, tracks: 0, size: 0, dirs: new Set(), leaf: true, miss: 0 });
    const f = m.get(name);
    f.tracks++; f.size += t.size; f.dirs.add(t.dir); if (rest !== name) f.leaf = false; if (missingOf(t).length) f.miss++;
  }
  return [...m.values()].sort((a, b) => a.name.localeCompare(b.name));
}
const qualityTag = (t) => (t.lossless ? '<span class="tag tertiary">Lossless</span>' : t.kbps ? `<span class="tag neutral">${t.kbps} kbps</span>` : '');

// ── Library columns ──────────────────────────────────────────────────────
// Each column knows how to render a cell and how to sort. Title is always shown; the rest are
// toggled from the cog in the table header and remembered in this browser.
const tagsPresent = (t) => 4 - missingOf(t).length;
const dash = '<span style="color:var(--md-outline)">—</span>';
const COLS = [
  { id: 'no', label: '#', name: 'Track number', w: '36px', def: true, sort: (t) => +t.no, cell: (t) => `<span class="mono l-m v">${t.no}</span>` },
  { id: 'title', label: 'Title', w: 'minmax(0,2.2fr)', fixed: true, def: true, sort: (t) => t.title.toLowerCase() },
  { id: 'artist', label: 'Artist', w: 'minmax(0,1.2fr)', sort: (t) => t.artist.toLowerCase(), cell: (t) => `<span class="b-m ell">${esc(t.artist)}</span>` },
  { id: 'album', label: 'Album', w: 'minmax(0,1.3fr)', def: true, sort: (t) => t.album.toLowerCase(), cell: (t) => `<span class="b-m ell">${esc(t.album)}</span>` },
  { id: 'year', label: 'Year', w: '56px', sort: (t) => t.year || 0, cell: (t) => (t.year ? `<span class="mono l-m">${t.year}</span>` : dash) },
  { id: 'genre', label: 'Genre', w: '104px', sort: (t) => (t.genre ? t.genreName.toLowerCase() : '~'), cell: (t) => (t.genre ? `<span class="b-m ell">${t.genreName}</span>` : dash) },
  { id: 'format', label: 'Format', w: '150px', def: true, sort: (t) => t.fmt + String(t.kbps).padStart(4, '0'), cell: (t) => `<span class="row" style="gap:8px"><span class="mono l-m">${t.fmt.toUpperCase()}</span>${qualityTag(t)}</span>` },
  { id: 'bpm', label: 'BPM', w: '56px', right: true, sort: (t) => (t.bpm ? t.bpmVal : -1), cell: (t) => (t.bpm ? `<span class="mono l-m">${t.bpmVal}</span>` : dash) },
  { id: 'tags', label: 'Tags', w: '110px', def: true, sort: tagsPresent, cell: (t) => { const m = missingOf(t); return `<span class="tags4" role="img" aria-label="${m.length ? 'Missing ' + m.join(', ') : 'All tags present'}">${TAG_ICONS.map(([k, i]) => `<span class="ms ${t[k] ? 'on' : 'off'}" aria-hidden="true">${i}</span>`).join('')}</span>`; } },
  { id: 'size', label: 'Size', w: '80px', right: true, sort: (t) => t.size, cell: (t) => `<span class="mono l-m v">${fmtMB(t.size)}</span>` },
  { id: 'len', label: 'Time', w: '56px', right: true, def: true, sort: (t) => t.len, cell: (t) => `<span class="mono l-m v">${fmtLen(t.len)}</span>` },
  { id: 'added', label: 'Added', w: '88px', sort: (t) => t.addedDays, cell: (t) => `<span class="b-m v">${t.addedDays === 0 ? 'Today' : t.addedDays + ' d ago'}</span>` },
  { id: 'isrc', label: 'ISRC', w: '132px', sort: (t) => t.isrc, cell: (t) => (t.isrc ? `<span class="mono v ell" style="font-size:12px">${esc(t.isrc)}</span>` : dash) },
  { id: 'file', label: 'File name', w: 'minmax(0,1.5fr)', sort: (t) => t.file.toLowerCase(), cell: (t) => { const bad = t.expected && t.file !== t.expected; return `<span class="mono ell ${bad ? '' : 'v'}" style="font-size:12px;${bad ? 'color:var(--md-error)' : ''}" title="${esc(t.file)}">${esc(t.file)}</span>`; } },
  { id: 'dir', label: 'Folder', w: 'minmax(0,1.5fr)', sort: (t) => t.dir.toLowerCase(), cell: (t) => `<span class="mono v ell" style="font-size:12px" title="${esc(t.dir)}">${esc(t.dir)}</span>` },
];
const DEFAULT_COLS = COLS.filter((c) => c.def).map((c) => c.id);
S.lib.cols = store.get('libcols', DEFAULT_COLS);
S.lib.sort = store.get('libsort', null);
const colsOn = () => COLS.filter((c) => c.fixed || S.lib.cols.includes(c.id));
const colTpl = (cols) => ['40px', ...cols.map((c) => c.w)].join(' ');
const colMinW = (cols) => 40 + cols.reduce((n, c) => n + (/^\d+px$/.test(c.w) ? parseInt(c.w, 10) : 150) + 12, 0);

function sortRows(list) {
  const s = S.lib.sort; const col = s && COLS.find((c) => c.id === s.k);
  if (!col) return list;
  const m = s.dir === 'desc' ? -1 : 1;
  return list.slice().sort((a, b) => { const x = col.sort(a), y = col.sort(b); return (x < y ? -1 : x > y ? 1 : 0) * m || a.path.localeCompare(b.path); });
}

function trackRow(t, cols) {
  const sel = S.lib.sel.has(t.id);
  const hasArtist = cols.some((c) => c.id === 'artist');
  const cells = cols.map((c) => c.id === 'title'
    ? `<button class="title" data-act="focus" data-id="${esc(t.id)}" style="flex-direction:row;align-items:center;gap:12px">${libCover(t)}<span class="col" style="min-width:0"><span class="b-l ell">${esc(t.title)}</span>${hasArtist ? '' : `<span class="b-m v ell">${esc(t.artist)}</span>`}</span></button>`
    : `<span class="c${c.right ? ' r' : ''}">${c.cell(t)}</span>`).join('');
  return `<div class="trow ${sel ? 'sel' : ''} ${S.lib.focus === t.id ? 'focus' : ''}" role="row" style="grid-template-columns:${colTpl(cols)}">
    <label class="cb"><input type="checkbox" data-act="sel" data-id="${esc(t.id)}" ${sel ? 'checked' : ''} aria-label="Select ${esc(t.title)}"></label>${cells}</div>`;
}
function folderRow(f, p, cols) {
  const path = (p ? p + '/' : '') + f.name;
  const cells = cols.map((c) => {
    if (c.id === 'title') return `<button class="title" data-act="cd" data-path="${esc(path)}" style="flex-direction:row;align-items:center;gap:12px">${ic('folder', 'f', 'color:var(--md-primary)')}<span class="col" style="min-width:0"><span class="b-l ell">${esc(f.name)}</span><span class="b-m v ell">${f.leaf ? plural(f.tracks, 'track') : plural(f.dirs.size, 'album') + ' · ' + plural(f.tracks, 'track')} · ${fmtMB(f.size)}</span></span></button>`;
    if (c.id === 'format' && f.miss) return `<span class="c"><span class="tag error">${f.miss} missing tags</span></span>`;
    return '<span class="c"></span>';
  }).join('');
  return `<div class="trow" role="row" style="grid-template-columns:${colTpl(cols)}"><span class="cb"></span>${cells}</div>`;
}
function tableHead(cols, allSel) {
  const s = S.lib.sort;
  const heads = cols.map((c) => {
    const on = s && s.k === c.id;
    return `<span role="columnheader" class="${c.id === 'title' ? '' : 'c'}${c.right ? ' r' : ''}" ${on ? `aria-sort="${s.dir === 'asc' ? 'ascending' : 'descending'}"` : ''}>
      <button class="th ${on ? 'on' : ''}" data-act="sort" data-k="${c.id}" title="Sort by ${esc(c.name || c.label)}">${esc(c.label)}${on ? ic(s.dir === 'asc' ? 'arrow_upward' : 'arrow_downward') : ''}</button></span>`;
  }).join('');
  return `<div class="trow head" role="row" style="grid-template-columns:${colTpl(cols)}"><label class="cb"><input type="checkbox" data-act="selall" ${allSel ? 'checked' : ''} aria-label="Select all visible tracks"></label>${heads}</div>`;
}
const colBody = () => `<div class="row wrap" style="gap:8px">${COLS.map((c) => { const on = c.fixed || S.lib.cols.includes(c.id);
  return `<button class="chip ${on ? 'on' : ''}" aria-pressed="${on}" data-act="coltoggle" data-k="${c.id}" ${c.fixed ? 'disabled title="Always shown"' : ''}>${on ? ic('check') : ''}${esc(c.name || c.label)}</button>`; }).join('')}</div>
  <div class="row" style="margin-top:16px"><button class="btn text" data-act="colreset" style="padding:0">Reset to default</button></div>`;

function detailHTML(t, compact = false) {
  const miss = missingOf(t);
  const dup = dupFor(t);
  const prop = (icon, k, v) => `<div class="p">${ic(icon, 'v', 'font-size:20px')}<span class="b-m v" style="width:96px">${k}</span><span class="b-m grow" style="word-break:break-word">${v}</span></div>`;
  const bad = (txt) => `<span class="tag error">${txt}</span>`;
  const sub = esc([t.artist, t.album, t.year].filter(Boolean).join(' · '));
  // Wide screens: big cover above the name. Phone dialog: a small cover beside the name and artist.
  const head = compact
    ? `<div class="row" style="gap:16px;align-items:center">${t.cover ? coverTile({ title: t.album || t.title, kind: 'track', url: coverUrl(t) }, 72)
        : `<span class="coverthumb" style="width:72px;height:72px;border-radius:14px;background:var(--md-sc-highest);color:var(--md-outline)" role="img" aria-label="No cover art">${ic('image_not_supported', '', 'font-size:32px')}</span>`}
        <div class="col grow" style="min-width:0"><h3 class="t-m" style="word-break:break-word;color:var(--md-on-surface)">${esc(t.title)}</h3><span class="b-m v">${sub}</span></div></div>`
    : `<div class="cover" style="position:relative;overflow:hidden;${t.cover ? `background:${coverGradient(t.album || t.title)};color:rgba(255,255,255,.8)` : 'background:var(--md-sc-highest);color:var(--md-outline)'}">${ic(t.cover ? 'album' : 'image_not_supported', '', 'font-size:64px')}<span class="l-m">${t.cover ? '' : 'No cover art'}</span>${t.cover ? `<img src="${esc(coverUrl(t))}" alt="Cover art" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover" onerror="this.remove()">` : ''}</div>
    <div class="col"><h2 class="hl-s" style="word-break:break-word">${esc(t.title)}</h2><span class="b-m v">${sub}</span></div>`;
  return `${head}
    <div class="props">
      ${prop('graphic_eq', 'Format', t.lossless ? `${t.fmt.toUpperCase()} · lossless` : `${t.fmt.toUpperCase()} · ${t.kbps} kbps`)}
      ${prop('sell', 'Genre', t.genre ? t.genreName : bad('Missing'))}
      ${prop('speed', 'BPM', t.bpm ? t.bpmVal : bad('Missing'))}
      ${prop('tag', 'ISRC', t.isrc ? `<span class="mono" style="font-size:12px">${esc(t.isrc)}</span>` : bad('Missing'))}
      ${prop('fingerprint', 'MusicBrainz', t.mbid ? 'Linked' : bad('Missing'))}
      ${prop('schedule', 'Length', `${fmtLen(t.len)} · ${fmtMB(t.size)}`)}
    </div>
    ${dup ? `<a href="#/health" class="tag tertiary" style="align-self:flex-start;height:32px;border-radius:8px;padding:0 12px">${ic('content_copy')}${plural(dup.files.length - 1, 'other copy', 'other copies')} — review</a>` : ''}
    ${t.expected && t.file !== t.expected ? `<span class="tag error" style="align-self:flex-start">${ic('text_fields')}Should be named “${esc(t.expected)}”</span>` : ''}
    <div class="pathbox mono">${esc(trackPath(t))}</div>
    <div class="row"><button class="btn ${miss.length ? 'filled' : 'tonal'} grow" data-act="enrich" data-id="${esc(t.id)}">${ic('auto_awesome')}${miss.length ? 'Enrich' : 'Re-enrich'}</button>
      <button class="btn outlined grow" data-act="dlfiles" data-id="${esc(t.id)}">${ic('download')}Download</button></div>`;
}

function libEmpty() {
  const L = S.lib;
  if (!L.loaded) return Array.from({ length: 6 }, () => `<div class="trow"><span></span><span class="skel" style="height:14px;width:60%;border-radius:4px;grid-column:2/-1"></span></div>`).join('');
  if (L.error) return emptyState('cloud_off', 'Couldn’t load the library', L.error);
  if (!L.ready) return emptyState('hourglass_top', 'Indexing your library…', 'The first scan can take a few minutes on a large library. This page fills in when it finishes.');
  return emptyState('folder_open', L.tracks.length ? 'Nothing matches' : 'No songs yet', L.tracks.length ? 'Clear a filter or pick another folder.' : 'Downloaded songs show up here.');
}
const libNotice = () => (S.lib.pending ? `<div class="row b-m" style="gap:12px;background:var(--md-sc-high);border-radius:16px;padding:10px 16px">${ic('hourglass_top', 'v')}<span class="grow">Reading tags… ${S.lib.pending.toLocaleString()} songs still loading. They appear as they finish.</span></div>` : '');

// The library is drawn in three parts so a keystroke or tap only redraws what changed:
// the shell (title, search box, view switch) on navigation, the body (filters, table, toolbar)
// on filter/sort/selection changes, and the aside (song details) when a song is picked.
// The table shows LIB_PAGE rows and adds more as the end scrolls into view.
const LIB_PAGE = 200;
function libRows() {
  const L = S.lib, p = L.path.join('/');
  const scope = libScope();
  const flat = L.view === 'tracks' || L.q.trim() || L.chips.size;
  const rows = sortRows(flat ? libFiltered(scope) : libFiltered(scope.filter((t) => t.dir === p)));
  const folders = flat ? [] : childFolders(p);
  if (L.sort && L.sort.k === 'title' && L.sort.dir === 'desc') folders.reverse();
  return { p, scope, rows, folders };
}
const libFiltersActive = () => S.lib.chips.size > 0 || S.lib.view !== 'tracks';
function libToolbar() {
  const L = S.lib;
  return L.sel.size ? `<div class="toolbar" role="toolbar" aria-label="Selected tracks"><span class="l-l" style="margin-right:12px">${L.sel.size} selected</span>
        <button class="btn filled" data-act="enrichsel">${ic('auto_awesome')}Enrich</button>
        <button class="ib" data-act="renamesel" title="Rename from tags" aria-label="Rename from tags">${ic('drive_file_rename_outline')}</button>
        <button class="ib" data-act="movesel" title="Move" aria-label="Move">${ic('drive_file_move')}</button>
        <button class="ib" data-act="dlsel" title="Download to browser" aria-label="Download to browser">${ic('download')}</button>
        <button class="ib" data-act="delsel" title="Delete" aria-label="Delete">${ic('delete')}</button>
        <button class="ib" data-act="clearsel" title="Clear selection" aria-label="Clear selection">${ic('close')}</button></div>` : '';
}
let libCur = null;   // the rows the table is currently showing, for "show more" and select-all
function libMoreHTML() {
  const left = libCur ? libCur.rows.length - libCur.shown : 0;
  return left > 0 ? `<div id="lib-more" class="row" style="justify-content:center;padding:12px"><button class="btn text" data-act="libmore">Show ${Math.min(left, LIB_PAGE)} more of ${left.toLocaleString()}</button></div>` : '';
}
function libBodyHTML() {
  const L = S.lib, cols = colsOn();
  const d = libRows();
  libCur = { ...d, cols, shown: Math.min(d.rows.length, LIB_PAGE) };
  const allSel = d.rows.length > 0 && d.rows.every((t) => L.sel.has(t.id));
  return `<div class="row wrap" style="gap:8px"><div class="row wrap lib-extra" style="gap:8px" role="group" aria-label="Filters">${CHIPS.map(([k, l]) => `<button class="chip ${L.chips.has(k) ? 'on' : ''}" aria-pressed="${L.chips.has(k)}" data-act="chip" data-k="${k}">${L.chips.has(k) ? ic('check') : ''}${l}</button>`).join('')}</div>
        <span class="b-m v" style="margin-left:auto">${plural(d.rows.length, 'track')}</span>
        <button class="ib" data-act="libreload" title="Rescan library" aria-label="Rescan library">${ic('refresh')}</button>
        <button class="ib hide-sm" data-act="cols" title="Choose columns" aria-label="Choose columns">${ic('settings')}</button></div>
      ${libNotice()}
      <div class="tscroll"><div role="table" aria-label="Tracks" class="tbl" id="lib-tbl" style="--minw:${colMinW(cols)}px">
        ${tableHead(cols, allSel)}
        ${d.folders.map((f) => folderRow(f, d.p, cols)).join('')}${d.rows.slice(0, libCur.shown).map((t) => trackRow(t, cols)).join('')}
        ${!d.folders.length && !d.rows.length ? libEmpty() : ''}
      </div>${libMoreHTML()}</div>
      <div id="lib-tools">${libToolbar()}</div>`;
}
// ── Cover-tinted details card ────────────────────────────────────────────
// The dominant colour of the cover becomes a Material scheme (same generator as the app theme) that is
// applied only inside the details card. Material's tone pairs keep the text readable, and the actual
// contrast is checked against the whole gradient before the tint is used; if it falls short the card
// keeps the normal theme.
const coverSeeds = new Map();
function coverSeed(url) {
  if (!MCU) return Promise.resolve(null);   // generator not loaded yet; retried once it is
  if (!coverSeeds.has(url)) coverSeeds.set(url, new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      try {
        const c = document.createElement('canvas'); c.width = c.height = 48;
        const x = c.getContext('2d', { willReadFrequently: true });
        x.drawImage(img, 0, 0, 48, 48);
        const d = x.getImageData(0, 0, 48, 48).data, px = [];
        for (let i = 0; i < d.length; i += 4) if (d[i + 3] >= 200) px.push(((255 << 24) | (d[i] << 16) | (d[i + 1] << 8) | d[i + 2]) >>> 0);
        if (px.length < 20) return resolve(null);
        const ranked = MCU.Score.score(MCU.QuantizerCelebi.quantize(px, 64));   // most "colourful and common" first
        // With no real colour in the picture (greys, black, white) Score answers with its stock blue; use a neutral
        // scheme from the picture's average colour instead of tinting everything blue.
        if (!ranked.length || ranked[0] === 0xff4285f4) {
          let r = 0, g = 0, b = 0;
          for (const p of px) { r += (p >> 16) & 255; g += (p >> 8) & 255; b += p & 255; }
          const h = (n) => Math.round(n / px.length).toString(16).padStart(2, '0');
          return resolve({ hex: `#${h(r)}${h(g)}${h(b)}`, neutral: true });
        }
        resolve({ hex: MCU.hexFromArgb(ranked[0]), neutral: false });
      } catch { resolve(null); }
    };
    img.onerror = () => resolve(null);
    img.src = url;
  }));
  return coverSeeds.get(url);
}
const lum = (hex) => { const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
const contrast = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
const TINT_VARS = Object.keys(ROLES);
function clearTint(el) {
  if (!el) return;
  TINT_VARS.forEach((k) => el.style.removeProperty('--md-' + k));
  el.style.removeProperty('background'); el.style.removeProperty('color'); delete el.dataset.tint;
}
async function tintEl(el, t) {
  if (!el) return;
  const seed = t && t.cover ? await coverSeed(coverUrl(t)) : null;
  const v = seed && schemeVars(seed.hex, seed.neutral);
  if (!v || (el.id === 'lib-aside' && (!t || S.lib.focus !== t.id))) return clearTint(el);
  const c = v[document.documentElement.dataset.mode];
  // Text sits on a gradient from primary-container (top) to surface (bottom): it must pass on both ends.
  const worst = Math.min(
    ...['on-surface', 'on-surface-variant'].flatMap((k) => [c['primary-container'], c.surface].map((bg) => contrast(c[k], bg))),
    contrast(c['on-primary-container'], c['primary-container']));
  if (worst < 4.5) return clearTint(el);
  TINT_VARS.forEach((k) => el.style.setProperty('--md-' + k, c[k]));
  el.style.background = `linear-gradient(165deg, ${c['primary-container']} 0%, ${c.surface} 62%)`;
  el.style.color = c['on-surface'];
  el.dataset.tint = worst.toFixed(1);
}
function tintDetail() {
  const el = $('#lib-aside'); if (!el) return;
  tintEl(el, S.lib.focus != null ? trackOf(S.lib.focus) : null);
}

function libAsideHTML() {
  const L = S.lib;
  const focus = L.focus != null ? trackOf(L.focus) : null;
  return focus ? detailHTML(focus) : emptyState('music_note', 'Select a track', 'Pick a song to see its tags, file path and duplicates.');
}
function viewLibrary() {
  const L = S.lib;
  const scope = libScope();
  const totalSize = scope.reduce((a, t) => a + t.size, 0);
  const crumbs = ['Library', ...L.path];
  return `<div class="lib-layout ${L.filtersOpen ? 'fopen' : ''}">
    <section class="pane grow" aria-labelledby="h-lib" style="padding:16px 16px 20px;display:flex;flex-direction:column;gap:14px;min-width:0">
      <div class="row wrap" style="gap:12px">
        ${L.path.length ? `<button class="ib" data-act="up" aria-label="Up one folder">${ic('arrow_back')}</button>` : ''}
        <div class="grow col"><h1 id="h-lib" class="hl-s ell">${esc(L.path.length ? L.path[L.path.length - 1] : 'Library')}</h1>
          <nav class="row wrap b-m v crumbs" aria-label="Breadcrumb">${crumbs.map((c, i) => i === crumbs.length - 1
            ? `<span>${esc(c)}</span>` : `<button class="btn text" style="height:28px;padding:0 6px" data-act="crumb" data-i="${i}">${esc(c)}</button><span>/</span>`).join('')}
            <span id="lib-total">· ${plural(scope.length, 'track')} · ${fmtMB(totalSize)}</span></nav></div>
        <label class="searchbar sm" style="width:300px;max-width:100%">${ic('search', 'v')}<input id="lq" type="search" enterkeyhint="search" value="${esc(L.q)}" placeholder="Search library" aria-label="Search library" data-input="lq" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">
          <button class="ib show-sm ${libFiltersActive() ? 'dotted' : ''}" data-act="libfilters" aria-expanded="${!!L.filtersOpen}" aria-label="Filters and view" title="Filters and view">${ic('tune')}</button></label>
        <div class="seg lib-extra" role="radiogroup" aria-label="View">
          <button role="radio" aria-checked="${L.view === 'folders'}" data-act="view" data-k="folders">${L.view === 'folders' ? ic('check') : ''}Folders</button>
          <button role="radio" aria-checked="${L.view === 'tracks'}" data-act="view" data-k="tracks">${L.view === 'tracks' ? ic('check') : ''}Tracks</button></div>
      </div>
      <div id="lib-body" class="col" style="gap:14px;min-width:0">${libBodyHTML()}</div>
    </section>
    <aside class="pane lib-aside" id="lib-aside" aria-label="Track details" style="width:360px;flex-shrink:0;padding:20px;display:flex;flex-direction:column;gap:16px;position:sticky;top:16px">
      ${libAsideHTML()}
    </aside></div>`;
}
// Redraw only the library body (and optionally the details panel); falls back to a full render elsewhere.
function renderLib({ aside = false } = {}) {
  const body = $('#lib-body');
  if (S.route !== 'library' || !body) return render();
  body.innerHTML = libBodyHTML();
  const tg = $('[data-act=libfilters]'); if (tg) tg.classList.toggle('dotted', libFiltersActive());
  if (aside) { $('#lib-aside').innerHTML = libAsideHTML(); tintDetail(); }
  watchLibMore();
}
function libAppend() {
  if (!libCur) return;
  const next = libCur.rows.slice(libCur.shown, libCur.shown + LIB_PAGE);
  libCur.shown += next.length;
  const tbl = $('#lib-tbl'); if (tbl) tbl.insertAdjacentHTML('beforeend', next.map((t) => trackRow(t, libCur.cols)).join(''));
  const more = $('#lib-more'); if (more) more.outerHTML = libMoreHTML();
  watchLibMore();
}
let libObserver;
function watchLibMore() {
  if (libObserver) libObserver.disconnect();
  const more = $('#lib-more');
  if (!more || !('IntersectionObserver' in window)) return;
  libObserver = new IntersectionObserver((ents) => { if (ents.some((x) => x.isIntersecting)) libAppend(); }, { rootMargin: '600px' });
  libObserver.observe(more);
}

// ═════════════════════════════════════════════════════════════════════════
// Library health
// ═════════════════════════════════════════════════════════════════════════
const KIND = { id: ['tag', 'Same ID'], tags: ['sell', 'Same tags'] };
const qLabel = (f) => (f.lossless ? 'Lossless' : `${Math.round((f.bitrate || 0) / 1000)} kbps`);
const MB = 1048576;

function mapDupGroups(res) {
  return (res.groups || []).map((g, gi) => {
    const f0 = g.files[0];
    return {
      id: 'd' + gi, kind: g.match === 'title' ? 'tags' : 'id', title: `${f0.artist} — ${f0.title}`,
      keepIdx: Math.max(0, g.files.findIndex((f) => f.rel === g.keep)),
      files: g.files.map((f) => ({ name: f.title, path: f.rel, fmt: f.ext.toUpperCase(), q: qLabel(f), size: f.size / MB, lossless: f.lossless })),
    };
  });
}
function mapMisGroups(res) {
  return (res.groups || []).map((g, gi) => ({
    id: 'm' + gi, kind: g.kind, sim: Math.round((g.sim || 0) * 100),
    files: g.files.map((f) => ({ name: f.title, artist: f.artist, path: f.rel, fmt: f.ext.toUpperCase(), q: qLabel(f), size: f.size / MB })),
  }));
}
async function loadScan(kind) {
  try {
    const d = await api(`/api/library/scan/${kind}`);
    const info = d.idle ? { idle: true } : { running: d.running, summary: d.summary, error: d.error, phase: d.phase };
    if (kind === 'dups') {
      S.h.dupsInfo = info; S.h.keep = {};
      S.h.dups = d.result ? mapDupGroups(d.result) : null;
    } else {
      S.h.misInfo = { ...info, fingerprint: d.result ? d.result.fingerprint : true, fp_reason: d.result ? d.result.fp_reason : '', fp_failed: d.result ? d.result.fp_failed : 0 };
      S.h.mis = d.result ? mapMisGroups(d.result) : null; S.h.sel.mis.clear();
    }
    render();
  } catch { /* leave what we have */ }
}
// One scan finds duplicates and mistagged songs together.
// The server patches stored scan results when files change, so just fetch them again (no rescan needed).
function refreshScans() { if (S.h.dups !== null) loadScan('dups'); if (S.h.mis !== null) loadScan('mistag'); }
const scanRunning = () => S.tasks.some((t) => t.running && t.id.startsWith('scan-'));
const keepOf = (g) => S.h.keep[g.id] ?? g.keepIdx;

function dupPlan() {
  let files = 0, mb = 0;
  for (const g of dupGroups()) {
    const k = keepOf(g);
    if (k === 'all') continue;
    g.files.forEach((f, i) => { if (i !== k) { files++; mb += f.size; } });
  }
  return { files, mb };
}
function fileRowDup(g, f, i) {
  const k = keepOf(g);
  const keep = k === 'all' || k === i;
  return `<label class="frow ${keep ? 'keep' : ''}"><span class="cb"><input type="radio" name="${g.id}" data-act="keep" data-g="${g.id}" data-i="${i}" ${keep && k !== 'all' ? 'checked' : ''} aria-label="Keep this copy"></span>
    <span class="col" style="min-width:0"><span class="b-l ell">${esc(f.name)}</span><span class="mono v ell" style="font-size:12px;line-height:18px">${esc(f.path)}</span></span>
    <span class="c-fmt row" style="gap:8px"><span class="mono l-m">${f.fmt} · ${fmtMB(f.size)}</span><span class="tag ${f.lossless ? 'tertiary' : 'neutral'}">${esc(f.q)}</span></span>
    <span class="tag ${keep ? 'primary' : 'error'}" style="justify-self:end">${ic(keep ? 'check' : 'delete')}${keep ? 'Keep' : 'Remove'}</span></label>`;
}

// A scan that hasn't produced a result yet: running, never run, or the library changed since.
function scanPending(kind, what, hint) {
  const info = kind === 'dups' ? S.h.dupsInfo : S.h.misInfo;
  if (scanRunning() || (info && info.running)) return emptyState('hourglass_top', 'Scanning your library…', 'One scan finds duplicates and mistagged songs. You can leave this page; the result is kept.');
  if (info && info.error) return emptyState('error', 'The scan failed', info.error);
  const stale = info && info.summary && /changed/i.test(info.summary);
  return `${emptyState(stale ? 'refresh' : 'radar', stale ? 'The library changed since the last scan' : `Not scanned yet`, hint)}
    <div class="row" style="justify-content:center"><button class="btn filled" data-act="scan">${ic('radar')}Scan library</button></div>`;
}

function healthDups() {
  if (S.h.dups === null) return scanPending('dups', 'duplicates', 'Looks for the same song saved more than once, even when the tags differ slightly.');
  const groups = S.h.dups.filter((g) => S.h.filters.has(g.kind));
  const plan = dupPlan();
  return `<div class="row wrap" style="gap:8px"><span class="b-m v grow">Pick the copy to keep in each group. The best copy is preselected: lossless first, then the most complete tags.</span>
      ${Object.entries(KIND).map(([k, [, l]]) => `<button class="chip ${S.h.filters.has(k) ? 'on' : ''}" aria-pressed="${S.h.filters.has(k)}" data-act="hfilter" data-k="${k}">${S.h.filters.has(k) ? ic('check') : ''}${l}</button>`).join('')}</div>
    ${groups.map((g) => `<fieldset class="group"><legend class="sr">${esc(g.title)}</legend>
      <div class="gh"><span class="t-m grow ell">${esc(g.title)}</span><span class="tag primary">${ic(KIND[g.kind][0])}${KIND[g.kind][1]}</span>
        <button class="btn text" data-act="keepall" data-g="${g.id}" aria-pressed="${S.h.keep[g.id] === 'all'}">Keep all</button></div>
      ${g.files.map((f, i) => fileRowDup(g, f, i)).join('')}</fieldset>`).join('') || emptyState('task_alt', S.h.dups.length ? 'No groups match these filters' : 'No duplicates', 'Nice and tidy.')}
    ${S.h.dups.length ? `<div class="dock"><span class="b-l grow"><strong style="font-weight:600">${plural(S.h.dups.length, 'group')}</strong><span class="v"> · ${plural(plan.files, 'file')} will be removed · ${fmtMB(plan.mb)} freed</span></span>
      <button class="btn text" data-act="skipall">Keep everything</button>
      <button class="btn filled lg" data-act="applydups" ${plan.files ? '' : 'disabled'}>${ic('done_all')}Apply decisions</button></div>` : ''}`;
}

function healthMis() {
  if (S.h.mis === null) return scanPending('mistag', 'mistagged songs', 'Compares what songs sound like (audio fingerprints) with their tags. This can take a while on a big library.');
  const sel = S.h.sel.mis; const info = S.h.misInfo || {};
  return `<span class="b-m v">Songs that sound the same but carry different tags — at least one holds the wrong song. Tick the ones to fix: repair deletes the file and downloads it again from its own tags.</span>
    ${info.fingerprint === false ? `<div class="row b-m" style="gap:12px;background:var(--md-error-container);color:var(--md-on-error-container);border-radius:16px;padding:10px 16px">${ic('warning')}<span class="grow">Audio fingerprinting isn’t available (${esc(info.fp_reason || 'unknown reason')}), so matches are based on length only and unverified.</span></div>` : ''}
    ${S.h.mis.map((g) => `<fieldset class="group"><legend class="sr">Group ${g.id}</legend>
      <div class="gh"><span class="t-m grow">${g.files.length} files, different tags</span><span class="tag ${g.kind === 'audio' ? 'primary' : 'neutral'}">${ic(g.kind === 'audio' ? 'graphic_eq' : 'straighten')}${g.kind === 'audio' ? `Same audio · ${g.sim}%` : 'Same length · unverified'}</span></div>
      ${g.files.map((f) => `<label class="frow ${sel.has(f.path) ? 'keep' : ''}"><span class="cb"><input type="checkbox" data-act="hsel" data-set="mis" data-id="${esc(f.path)}" ${sel.has(f.path) ? 'checked' : ''} aria-label="Select ${esc(f.name)}"></span>
        <span class="col" style="min-width:0"><span class="b-l ell">${esc(f.artist)} — ${esc(f.name)}</span><span class="mono v ell" style="font-size:12px;line-height:18px">${esc(f.path)}</span></span>
        <span class="c-fmt row" style="gap:8px"><span class="mono l-m">${f.fmt}</span><span class="tag neutral">${esc(f.q)}</span></span><span class="mono l-m v" style="justify-self:end">${fmtMB(f.size)}</span></label>`).join('')}</fieldset>`).join('') || emptyState('task_alt', 'Nothing mistagged')}
    ${S.h.mis.length ? `<div class="dock"><span class="b-l grow">${sel.size ? plural(sel.size, 'file') + ' selected' : 'Select files to fix'}</span>
      <button class="btn tonal" data-act="repair" ${sel.size ? '' : 'disabled'}>${ic('build')}Repair</button>
      <button class="btn outlined danger" data-act="hdelete" ${sel.size ? '' : 'disabled'}>${ic('delete')}Delete</button></div>` : ''}`;
}

function listRows(kind, list, renderBody) {
  const sel = S.h.sel[kind];
  const all = list.length > 0 && list.every((t) => sel.has(t.id));
  return `<div class="row"><label class="row b-m" style="gap:0"><span class="cb"><input type="checkbox" data-act="hselall" data-set="${kind}" ${all ? 'checked' : ''} aria-label="Select all"></span>Select all</label></div>
    <div class="col" style="gap:2px">${list.map((t) => `<label class="li" style="min-height:64px;cursor:pointer;${sel.has(t.id) ? 'background:var(--md-secondary-container)' : ''}">
      <span class="cb"><input type="checkbox" data-act="hsel" data-set="${kind}" data-id="${esc(t.id)}" ${sel.has(t.id) ? 'checked' : ''} aria-label="Select ${esc(t.title)}"></span>${renderBody(t)}</label>`).join('')}</div>`;
}
function libGate(body) {
  if (!S.lib.loaded) return emptyState('hourglass_top', 'Loading your library…');
  if (S.lib.error) return emptyState('cloud_off', 'Couldn’t load the library', S.lib.error);
  if (!S.lib.ready) return emptyState('hourglass_top', 'Indexing your library…', 'This fills in when the first scan finishes.');
  return body();
}
function healthNames() {
  return libGate(() => {
    const list = misnamed(); const sel = S.h.sel.name;
    return `<span class="b-m v">These file names don’t match what your naming format makes of their tags. Renaming fixes the file, not the tags. To move files into new folders, use Organize in the <a href="/classic">classic interface</a>.</span>
    ${list.length ? listRows('name', list, (t) => `<span class="col grow" style="min-width:0"><span class="b-l ell">${esc(t.expected)}</span><span class="mono v ell" style="font-size:12px">now: ${esc(t.file)}</span></span><span class="b-s v hide-sm ell" style="max-width:220px">${esc(t.dir)}</span>`)
      : emptyState('task_alt', 'All file names match their tags')}
    ${list.length ? `<div class="dock"><span class="b-l grow">${sel.size ? plural(sel.size, 'file') + ' selected' : plural(list.length, 'file') + ' to rename'}</span>
      <button class="btn filled" data-act="renamenames">${ic('drive_file_rename_outline')}${sel.size ? 'Rename selected' : 'Rename all'}</button></div>` : ''}`;
  });
}
function healthMissing() {
  return libGate(() => {
    const list = missingList(); const sel = S.h.sel.miss;
    return `<span class="b-m v">Songs without a genre, BPM, MusicBrainz ID or cover. Enrich looks them up and writes the tags.</span>
    ${list.length ? listRows('miss', list, (t) => `<span class="col grow" style="min-width:0"><span class="b-l ell">${esc(t.title)}</span><span class="b-m v ell">${esc(t.artist)} · ${esc(t.album)}</span></span>
      <span class="row wrap" style="gap:4px;justify-content:flex-end">${missingOf(t).map((k) => `<span class="tag error">No ${k === 'mbid' ? 'MusicBrainz ID' : k}</span>`).join('')}</span>`)
      : emptyState('task_alt', 'Every song is fully tagged')}
    ${list.length ? `<div class="dock"><span class="b-l grow">${sel.size ? plural(sel.size, 'song') + ' selected' : plural(list.length, 'song') + ' need tags'}</span>
      <button class="btn filled" data-act="enrichmiss">${ic('auto_awesome')}${sel.size ? 'Enrich selected' : 'Enrich all'}</button></div>` : ''}`;
  });
}

function viewHealth() {
  const tabs = [['dups', 'content_copy', 'Duplicates', S.h.dups ? S.h.dups.length : '–'], ['mis', 'graphic_eq', 'Mistagged', S.h.mis ? S.h.mis.length : '–'],
    ['name', 'text_fields', 'Misnamed', S.lib.loaded && S.lib.ready ? misnamed().length : '–'], ['miss', 'label_off', 'Missing tags', S.lib.loaded && S.lib.ready ? missingList().length : '–']];
  const body = { dups: healthDups, mis: healthMis, name: healthNames, miss: healthMissing }[S.h.tab]();
  const b = scanInfo();
  const busy = !!b;
  return `<section class="pane" style="padding:24px 28px 20px;display:flex;flex-direction:column;gap:16px" aria-labelledby="h-health">
    <header class="row wrap" style="align-items:flex-end;gap:16px"><div class="grow col" style="gap:4px"><h1 id="h-health" class="hl-m">Library health</h1>
      <span class="b-m v">One scan finds duplicates and mistagged songs. Misnamed files and missing tags are read live from your library.</span></div>
      <button class="btn tonal" data-act="scan" ${busy ? 'disabled' : ''}>${busy ? `<span class="ms spin" aria-hidden="true">progress_activity</span><span data-scan-health>${esc(b ? b.text : 'Scanning…')}</span>` : `${ic('radar')}Scan library`}</button></header>
    ${busy ? `<div class="lp ${b && b.pct != null ? '' : 'ind'}" role="progressbar" aria-label="Scan progress" data-scan-bar><span class="a" style="width:${b && b.pct != null ? b.pct : 30}%"></span><span class="t"></span></div>` : ''}
    <div class="tabs" role="tablist" aria-label="Issue type">${tabs.map(([k, i, l, n]) => `<button class="tab" role="tab" aria-selected="${S.h.tab === k}" data-act="htab" data-k="${k}">${ic(i, S.h.tab === k ? 'f' : '')}<span class="t-s">${l}</span><span class="tag ${S.h.tab === k ? 'primary' : 'neutral'}" style="height:20px;padding:0 8px">${n}</span></button>`).join('')}</div>
    ${body}</section>`;
}

// ═════════════════════════════════════════════════════════════════════════
// Settings
// ═════════════════════════════════════════════════════════════════════════
const SECTIONS = [['appearance', 'palette', 'Appearance'], ['naming', 'text_fields', 'File naming'], ['downloads', 'download', 'Downloads'], ['sources', 'hub', 'Sources'],
  ['extensions', 'extension', 'Extensions'], ['metadata', 'sell', 'Metadata'], ['lb', 'queue_music', 'ListenBrainz'], ['network', 'vpn_lock', 'Network & VPN']];
const TOKENS = ['{artist}', '{album}', '{title}', '{track}', '{year}'];
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

const field = (label, key, { suffix = '', help = '', type = 'text', min, id } = {}) =>
  `<div class="field"><div class="box"><label for="f-${id || key}">${label}</label>
    <input id="f-${id || key}" type="${type}" ${min != null ? `min="${min}"` : ''} value="${esc(getPath(S.set, key))}" data-input="${type === 'number' ? 'num' : 'text'}" data-key="${key}" autocomplete="off">
    ${suffix ? `<span class="b-l v">${suffix}</span>` : ''}</div>${help ? `<span class="help">${help}</span>` : ''}</div>`;
const switchBtn = (path, label) => `<button class="sw" role="switch" aria-checked="${!!getPath(S.set, path)}" aria-label="${esc(label)}" data-act="tog" data-path="${path}"><span class="th">${getPath(S.set, path) ? ic('check') : ''}</span></button>`;
const switchRow = (path, label, help) => `<div class="row" style="min-height:56px;gap:16px"><span class="grow col"><span class="b-l">${label}</span>${help ? `<span class="b-m v">${help}</span>` : ''}</span>${switchBtn(path, label)}</div>`;

function slider(key, min, max, label, help) {
  const v = S.set[key]; const pct = ((v - min) / (max - min)) * 100;
  return `<div class="col" style="gap:4px"><div class="row" style="align-items:baseline"><span class="t-s grow">${label}</span><span class="b-s v">${help}</span></div>
    <div class="slider-wrap"><output id="out-${key}" style="left:calc(${pct}% + ${((0.5 - pct / 100) * 4).toFixed(2)}px)">${v}</output>
    <input type="range" class="slider" min="${min}" max="${max}" step="1" value="${v}" aria-label="${label}" data-input="slider" data-key="${key}" data-min="${min}" data-max="${max}" style="--pct:${pct}%"></div></div>`;
}
const fmtPreview = () => S.set.fmt.replace('{artist}', 'Bee Gees').replace('{album}', 'Greatest').replace('{title}', "Stayin' Alive").replace('{track}', '05').replace('{year}', '1979') + '.flac';

function schemePicker() {
  const cur = schemeId(); const mode = document.documentElement.dataset.mode;
  const grad = (seed, neutral) => { const v = schemeVars(seed, neutral); return v ? `linear-gradient(135deg,${v[mode].primary} 49%,${v[mode]['primary-container']} 51%)` : 'var(--md-sc-high)'; };
  const item = (id, name, seed, neutral) => `<div class="swc"><button class="swatch ${cur === id ? 'on' : ''}" role="radio" aria-checked="${cur === id}" aria-label="${name}" data-act="scheme" data-k="${id}" style="background:${grad(seed, neutral)}">${cur === id ? ic('check', 'f', 'color:#fff;background:rgba(0,0,0,.45);border-radius:12px;font-size:20px') : ''}</button><span class="b-s">${name}</span></div>`;
  const device = DEVICE ? item('device', 'Device', DEVICE, false)
    : `<div class="swc"><button class="swatch" disabled aria-label="Device colour, not available in this browser" style="background:var(--md-sc-highest);color:var(--md-outline)">${ic('smartphone')}</button><span class="b-s v">Device<br>unavailable</span></div>`;
  const seed = store.get('seed', '#6750A4');
  const custom = `<div class="swc"><label class="swatch ${cur === 'custom' ? 'on' : ''}" style="background:${cur === 'custom' ? grad(seed, false) : 'conic-gradient(#e53935,#fdd835,#43a047,#1e88e5,#8e24aa,#e53935)'};cursor:pointer;color:#fff">${ic('palette', '', 'text-shadow:0 1px 3px rgba(0,0,0,.6)')}
    <input type="color" class="sr" value="${seed}" data-input="seed" data-change="seed" aria-label="Custom colour"></label><span class="b-s">Custom</span></div>`;
  return `<div class="col" style="gap:12px"><span class="t-s">Colour scheme</span>
    <p class="b-m v">${DEVICE ? `Following your device colour (<span class="mono">${DEVICE}</span>) until you pick another.` : 'Your browser doesn’t share a device colour, so Neutral is the default.'}</p>
    <div class="swatches" role="radiogroup" aria-label="Colour scheme">${device}${SCHEMES.map((s) => item(s.id, s.name, s.seed, s.neutral)).join('')}${custom}</div></div>`;
}

const area = (label, key, { help = '', rows = 3 } = {}) =>
  `<div class="field"><div class="box" style="height:auto;padding:14px 16px 10px"><label for="f-${key}">${label}</label>
    <textarea id="f-${key}" rows="${rows}" data-input="text" data-key="${key}" spellcheck="false" style="flex-grow:1;min-width:0;background:none;border:none;outline:none;resize:vertical;color:var(--md-on-surface);font:inherit;font-size:14px">${esc(getPath(S.set, key))}</textarea></div>${help ? `<span class="help">${help}</span>` : ''}</div>`;

// Source health: installed extensions (from /api/extensions/status) and success rates (from /api/providers).
function sourceHealth(id) {
  if (S.ext && !(S.ext.installed_services || []).includes(id)) return { text: 'Extension not installed', tone: 'off' };
  const p = S.providers.find((x) => x.name === id || String(x.name).includes(id));
  if (!p) return { text: S.ext ? 'No downloads yet' : 'Checking…', tone: 'off' };
  const rate = p.rate != null ? ` · ${p.rate}% success` : '';
  if (p.health === 'good') return { text: 'Healthy' + rate, tone: 'ok' };
  if (p.health === 'degraded') return { text: 'Slow' + rate, tone: 'bad' };
  if (p.health === 'bad') return { text: 'Failing' + rate, tone: 'bad' };
  return { text: 'No recent data' + rate, tone: 'off' };
}
const toneColor = (t) => (t === 'bad' ? 'var(--md-error)' : 'var(--md-on-surface-variant)');
const taskDetail = (id) => (S.tasks.find((t) => t.id === id) || {}).detail || '';

const SETTINGS = {
  appearance: () => `<section class="section" aria-labelledby="h-set"><h2 id="h-set" class="t-l">Appearance</h2>
    ${schemePicker()}
    <div class="col" style="gap:12px"><span class="t-s">Mode</span><div class="seg" role="radiogroup" aria-label="Mode">
      ${[['auto', 'Auto'], ['light', 'Light'], ['dark', 'Dark']].map(([k, l]) => `<button role="radio" aria-checked="${store.get('mode', 'auto') === k}" data-act="mode" data-k="${k}">${store.get('mode', 'auto') === k ? ic('check') : ''}${l}</button>`).join('')}</div></div>
    <div class="li" style="background:var(--md-sc-low);border-radius:20px">${ic('info', 'v')}<span class="grow col"><span class="b-l">SpotiFLAC ${S.ver ? esc(S.ver.installed) : ''}</span><span class="b-m v">${S.ver && S.ver.update_available ? `Version ${esc(S.ver.latest)} is available` : 'Colour and mode are saved in this browser.'}</span></span>
      <a class="btn outlined" href="/classic">Classic interface</a></div></section>`,
  naming: () => `<section class="section" aria-labelledby="h-set"><h2 id="h-set" class="t-l">File naming</h2>
    ${field('Path template', 'fmt', { help: 'Use / to make subfolders' })}
    <div class="row wrap" style="gap:8px">${TOKENS.map((t) => `<button class="chip mono" data-act="token" data-t="${t}">${ic('add')}${t}</button>`).join('')}</div>
    <div class="li" style="background:var(--md-sc-low);border-radius:16px;min-height:64px">${ic('folder_open', 'v')}<span class="col"><span class="l-m v">Preview</span><span class="mono" id="fmt-preview" style="font-size:13px">${esc(fmtPreview())}</span></span></div>
    <div class="col" style="gap:8px"><span class="t-s">M3U playlists</span><div class="seg" role="radiogroup" aria-label="M3U playlists">
      ${[['always', 'Always'], ['ask', 'Ask'], ['never', 'Never']].map(([k, l]) => `<button role="radio" aria-checked="${S.set.m3u === k}" data-act="m3u" data-k="${k}">${S.set.m3u === k ? ic('check') : ''}${l}</button>`).join('')}</div>
      <span class="b-s v">Whether to write an .m3u file in a Playlists/ folder for playlist downloads</span></div></section>`,
  downloads: () => `<section class="section" aria-labelledby="h-set"><h2 id="h-set" class="t-l">Downloads</h2>
    ${slider('parallel', 1, 8, 'Parallel jobs', 'Albums and playlists downloading at once')}
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px 16px">
      ${field('Delay between tracks', 'delay', { type: 'number', min: 0, suffix: 's', help: 'Raise this if a source rate-limits you' })}
      ${field('Auto-retry every', 'retryEvery', { type: 'number', min: 0, suffix: 'min', help: '0 turns automatic retries off' })}
      ${field('Maximum retries', 'maxRetries', { type: 'number', min: 0, suffix: '×', help: '0 = keep retrying' })}
      ${field('Source timeout', 'timeout', { type: 'number', min: 0, suffix: 's', help: '0 = source default (120 s). Sources that solve a browser challenge may need longer.' })}</div></section>`,
  sources: () => `<section class="section" aria-labelledby="h-set"><div class="row" style="align-items:baseline"><h2 id="h-set" class="t-l grow">Sources</h2><span class="b-s v">Drag to set the order they are tried in</span></div>
    <div class="col" style="gap:2px" id="src-list">${S.set.sources.map((s, i, arr) => { const h = sourceHealth(s.id); return `<div class="srow ${s.on ? '' : 'off'}" draggable="true" data-src="${s.id}">
      <span class="ms v" aria-hidden="true" style="cursor:grab">drag_indicator</span><span class="av t-m">${i + 1}</span>
      <span class="grow col"><span class="b-l">${s.name}</span><span class="b-m" style="color:${toneColor(h.tone)}">${esc(h.text)}</span></span>
      <span class="reorder"><button class="ib" data-act="srcmove" data-id="${s.id}" data-d="-1" aria-label="Move ${s.name} up" ${i === 0 ? 'disabled' : ''}>${ic('keyboard_arrow_up')}</button>
        <button class="ib" data-act="srcmove" data-id="${s.id}" data-d="1" aria-label="Move ${s.name} down" ${i === arr.length - 1 ? 'disabled' : ''}>${ic('keyboard_arrow_down')}</button></span>
      <button class="sw" role="switch" aria-checked="${s.on}" aria-label="Use ${s.name}" data-act="srcon" data-id="${s.id}"><span class="th">${s.on ? ic('check') : ''}</span></button></div>`; }).join('')}</div>
    ${field('Qobuz token', 'qobuz', { type: 'password', help: 'Only needed for the Qobuz source' })}</section>`,
  extensions: () => `<section class="section" aria-labelledby="h-set"><h2 id="h-set" class="t-l">Extensions</h2>
    <p class="b-m v">SpotiFLAC ships no download providers of its own. Paste a registry URL you trust (one per line), save, then install. Nothing is fetched automatically.</p>
    ${area('Registry URLs', 'registry', { help: 'https://…/registry.json' })}
    <div class="row wrap"><button class="btn filled" data-act="extrefresh">${ic('download')}Install / refresh extensions</button></div>
    <h3 class="t-m" style="margin-top:8px">Provider health</h3>
    <div class="col" style="gap:2px">${S.set.sources.map((s) => { const h = sourceHealth(s.id); return `<div class="srow" style="padding-left:16px"><span class="ms" aria-hidden="true" style="color:${h.tone === 'ok' ? 'var(--md-primary)' : h.tone === 'bad' ? 'var(--md-error)' : 'var(--md-outline)'}">${h.tone === 'ok' ? 'check_circle' : h.tone === 'bad' ? 'warning' : 'radio_button_unchecked'}</span>
      <span class="grow col"><span class="b-l">${s.name}</span><span class="b-m v">${esc(h.text)}</span></span></div>`; }).join('')}</div>
    <div class="row wrap"><button class="btn tonal" data-act="tidalrefresh">${ic('refresh')}Refresh Tidal APIs</button><button class="btn outlined" data-act="resetstats">Reset stats</button></div></section>`,
  metadata: () => `<section class="section" aria-labelledby="h-set"><h2 id="h-set" class="t-l">Metadata</h2>
    ${switchRow('meta.on', 'Fetch extra tags after download', 'Genre, label, BPM and UPC. Providers are queried in parallel and the first answer per field wins.')}
    <div class="col" style="${S.set.meta.on ? '' : 'opacity:.5;pointer-events:none'}">${[['deezer', 'Deezer', 'Free'], ['apple', 'Apple Music', 'Free'], ['qobuz', 'Qobuz', ''], ['tidal', 'Tidal', ''], ['mb', 'MusicBrainz genre fallback', 'Rate-limited to 1 request per second']].map(([k, l, h]) => switchRow('meta.' + k, l, h)).join('')}</div>
    <h3 class="t-m" style="margin-top:8px">Library index</h3>
    <div class="li" style="background:var(--md-sc-low);border-radius:20px">${ic('database', 'v')}<span class="grow col"><span class="b-l">${esc(taskDetail('lib-index') || 'Index status loading…')}</span><span class="b-m v">Used for “In library” badges in search results</span></span><button class="btn tonal" data-act="reindex">${ic('refresh')}Rescan</button></div></section>`,
  lb: () => `<section class="section" aria-labelledby="h-set"><h2 id="h-set" class="t-l">ListenBrainz</h2>
    ${switchRow('lb.on', 'Auto-download recommendation playlists', 'Tracks already in your library are skipped')}
    ${field('ListenBrainz username', 'lb.user', { id: 'lbuser', help: 'Whose recommendation playlists to follow' })}
    <div class="col" style="gap:8px"><span class="t-s">Sync on</span><div class="row wrap" role="group" aria-label="Days" style="gap:8px">${DAYS.map((d, i) => `<button class="chip ${S.set.lb.days.includes(i) ? 'on' : ''}" aria-pressed="${S.set.lb.days.includes(i)}" data-act="day" data-d="${i}">${S.set.lb.days.includes(i) ? ic('check') : ''}${d}</button>`).join('')}</div></div>
    <div style="max-width:200px">${field('At', 'lb.time', { type: 'time', id: 'lbtime' })}</div>
    <div class="li" style="background:var(--md-sc-low);border-radius:20px">${ic('sync', 'v')}<span class="grow col"><span class="b-l">Last sync</span><span class="b-m v">${esc(taskDetail('lb-sync') || 'No sync yet')}</span></span><button class="btn tonal" data-act="lbsync">${ic('sync')}Sync now</button></div></section>`,
  network: () => `<section class="section" aria-labelledby="h-set"><h2 id="h-set" class="t-l">Network &amp; VPN</h2>
    <div class="li vpn-card ${S.vpn.known ? (S.vpn.on ? 'on' : 'off') : ''}" style="border-radius:20px">${ic(S.vpn.on ? 'vpn_lock' : 'vpn_key_off', 'f')}<span class="grow col"><span class="b-l">${!S.vpn.known ? 'Checking VPN…' : S.vpn.on ? 'VPN connected' : 'VPN not connected'}</span><span class="b-m">${S.vpn.on && S.vpn.since ? 'Up for ' + span(Date.now() / 1000 - S.vpn.since) : S.vpn.known && !S.vpn.on ? 'Using your normal connection' : ''}</span></span><button class="btn tonal" data-act="vpn">Details</button></div>
    ${field('Reconnect the VPN after', 'reconnect', { type: 'number', min: 0, suffix: 'failures', help: 'Consecutive downloads where every source failed. 0 = never.' })}</section>`,
};

function viewSettings() {
  return `<div class="set-layout">
    <nav class="pane set-nav" aria-label="Settings sections" style="width:300px;flex-shrink:0;padding:20px 12px;display:flex;flex-direction:column;gap:2px;position:sticky;top:16px">
      <h1 class="hl-s" style="padding:4px 16px 16px">Settings</h1>
      ${SECTIONS.map(([k, i, l]) => `<button class="snav ${S.section === k ? 'on' : ''}" data-act="section" data-k="${k}" ${S.section === k ? 'aria-current="true"' : ''}>${ic(i)}<span class="l-l">${l}</span></button>`).join('')}</nav>
    <div class="pane grow pad-sm" style="padding:28px 40px;min-width:0;min-height:480px">${SETTINGS[S.section]()}</div></div>`;
}

// ═════════════════════════════════════════════════════════════════════════
// Router + render
// ═════════════════════════════════════════════════════════════════════════
const VIEWS = { download: viewDownload, library: viewLibrary, health: viewHealth, settings: viewSettings };
const TITLES = { download: 'Download', library: 'Library', health: 'Library health', settings: 'Settings' };

// Sidebar, task chip and phone top bar: cheap, and safe to redraw while someone is typing in the page.
function renderChrome() {
  $('#rail').innerHTML = railHTML();
  $('#bg').innerHTML = scanChip();
  $('#mtop').innerHTML = `<div class="row" style="gap:10px">${brandHTML()}</div><span class="grow"></span>${vpnPill()}`;
}
function render() {
  const a = document.activeElement;
  const fid = a && a.id && a.closest('#view') ? a.id : null;
  const caret = fid && typeof a.selectionStart === 'number' ? [a.selectionStart, a.selectionEnd] : null;
  const y = window.scrollY;
  renderChrome();
  $('#view').innerHTML = VIEWS[S.route]();
  if (S.route === 'library') { watchLibMore(); tintDetail(); }
  if (fid) {
    const n = document.getElementById(fid);
    if (n) { n.focus({ preventScroll: true }); if (caret) { try { n.setSelectionRange(caret[0], caret[1]); } catch { /* not a text field */ } } }
  }
  window.scrollTo(0, y);
  document.title = `SpotiFLAC — ${TITLES[S.route]}`;
}

function go(route) {
  if (location.hash === '#/' + route) render(); else location.hash = '#/' + route;
}
function onRoute() {
  const r = location.hash.replace(/^#\//, '');
  S.route = VIEWS[r] ? r : 'download';
  render();
  enterRoute();
  $('#view').focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

// ═════════════════════════════════════════════════════════════════════════
// Actions
// ═════════════════════════════════════════════════════════════════════════
const A = {};
const idsOf = (el) => (el && el.dataset.id ? [el.dataset.id] : [...S.lib.sel]);
const baseName = (p) => p.slice(p.lastIndexOf('/') + 1);
const trackOf = (path) => S.lib.tracks.find((t) => t.path === path);
// The server re-indexes in the background after file changes, so look again a moment later.
const later = (ms = 2500) => setTimeout(() => loadTracks(true), ms);
const firstError = (d) => (d && d.errors && d.errors[0]) || '';
function downloadFile(href) { const a = document.createElement('a'); a.href = href; a.rel = 'noopener'; document.body.appendChild(a); a.click(); a.remove(); }

async function enrichPaths(paths) {
  if (!paths.length) return;
  try {
    await api('/api/library/enrich', { method: 'POST', body: { paths } });
    snack(`Enriching ${plural(paths.length, 'song')}…`);
    refreshTasks();
  } catch (e) { oops('Couldn’t start enrichment')(e); }
}

// Download
A.paste = () => {
  go('download');
  setTimeout(() => {
    const q = $('#q'); if (q) q.focus();
    if (navigator.clipboard && navigator.clipboard.readText) navigator.clipboard.readText().then((t) => { if (t && LINK_RE.test(t)) { S.q = t.trim(); scheduleSearch(); render(); const q2 = $('#q'); if (q2) q2.focus(); } }).catch(() => {});
  }, 0);
};
A.pasteq = () => { navigator.clipboard?.readText().then((t) => { if (t) { S.q = t.trim(); scheduleSearch(); render(); $('#q')?.focus(); } }).catch(() => snack('Clipboard is blocked by the browser — paste with Ctrl+V')); };
A.clearq = () => { S.q = ''; scheduleSearch(); render(); $('#q')?.focus(); };
A.type = (el) => { S.type = el.dataset.k; renderResults(); };
A.quality = (el) => { S.quality = el.dataset.k; store.set('quality', S.quality); render(); saved(); };
A.dl = (el) => {
  const r = S.results[+el.dataset.i];
  if (S.inLib[r.url] === 'full') snack(`“${r.title}” is already in your library`, { label: 'Download anyway', fn: () => enqueue(r) });
  else enqueue(r);
};
A.dllink = (el) => enqueue({ url: S.q.trim(), title: '', type: el.dataset.type });
A.more = () => runSearch(S.resultsFor || S.q.trim(), S.nextOffset);
const jobAct = (path, method, fail) => async (el) => { try { await api(path(el.dataset.id), { method }); refreshJobs(); } catch (e) { oops(fail)(e); } };
A.jcancel = jobAct((id) => `/api/jobs/${encodeURIComponent(id)}/cancel`, 'POST', 'Couldn’t cancel');
A.jretry = jobAct((id) => `/api/jobs/${encodeURIComponent(id)}/retry`, 'POST', 'Couldn’t retry');
A.jremove = jobAct((id) => `/api/jobs/${encodeURIComponent(id)}`, 'DELETE', 'Couldn’t remove');
A.retryall = async () => {
  const failed = S.jobs.filter((j) => j.status === 'error');
  await Promise.all(failed.map((j) => api(`/api/jobs/${encodeURIComponent(j.id)}/retry`, { method: 'POST' }).catch(() => null)));
  snack(`Retrying ${plural(failed.length, 'download')}`); refreshJobs();
};
A.cleardone = async () => {
  const n = doneJobs().length;
  try { await api('/api/jobs', { method: 'DELETE' }); snack(`Cleared ${plural(n, 'finished download')}`); refreshJobs(); } catch (e) { oops('Couldn’t clear')(e); }
};
A.doneopen = () => { S.doneAll = !S.doneAll; render(); };
A.dldone = (el) => downloadFile(`/api/jobs/${encodeURIComponent(el.dataset.id)}/download`);
A.jretrypart = async (el) => {
  const j = S.jobs.find((x) => x.id === el.dataset.id);
  const urls = (j.track_results || []).filter((r) => r.success === false || r.status === 'failed').map((r) => `https://open.spotify.com/track/${r.track_id}`);
  if (!urls.length) return snack('No failed tracks to retry');
  try {
    await api(`/api/jobs/${encodeURIComponent(j.id)}/retry-partial`, { method: 'POST', body: { urls: urls.join('\n'), pre_success_count: j.success_count || 0, full_total: j.total || 0 } });
    snack(`Retrying ${plural(urls.length, 'failed track')}`); refreshJobs();
  } catch (e) { oops('Couldn’t retry')(e); }
};

// Library
A.cd = (el) => { S.lib.path = el.dataset.path.split('/'); S.lib.focus = null; render(); };
A.up = () => { S.lib.path.pop(); render(); };
A.crumb = (el) => { S.lib.path = S.lib.path.slice(0, +el.dataset.i); render(); };
A.view = (el) => { S.lib.view = el.dataset.k; render(); };
A.chip = (el) => { const c = S.lib.chips; c.has(el.dataset.k) ? c.delete(el.dataset.k) : c.add(el.dataset.k); renderLib(); };
A.libmore = () => libAppend();
// Phone only: the view switch and filter chips are folded away until asked for.
A.libfilters = (el) => {
  S.lib.filtersOpen = !S.lib.filtersOpen;
  const lay = $('.lib-layout'); if (lay) lay.classList.toggle('fopen', S.lib.filtersOpen);
  el.setAttribute('aria-expanded', String(S.lib.filtersOpen));
};
// Ticking a song only touches its row, the toolbar and the select-all box.
function libSelPatch() {
  const tools = $('#lib-tools'); if (tools) tools.innerHTML = libToolbar();
  const all = $('#lib-body [data-act=selall]');
  if (all && libCur) all.checked = libCur.rows.length > 0 && libCur.rows.every((t) => S.lib.sel.has(t.id));
  if (S.route === 'library') { const fab = $('.rail .mfab'); if (fab) fab.hidden = S.lib.sel.size > 0; }
}
A.sel = (el) => {
  const id = el.dataset.id; el.checked ? S.lib.sel.add(id) : S.lib.sel.delete(id);
  const row = el.closest('.trow'); if (row) row.classList.toggle('sel', el.checked);
  libSelPatch();
};
A.sort = (el) => {
  const k = el.dataset.k, s = S.lib.sort;
  S.lib.sort = !s || s.k !== k ? { k, dir: 'asc' } : s.dir === 'asc' ? { k, dir: 'desc' } : null;
  store.set('libsort', S.lib.sort); renderLib();
};
A.cols = () => dialog('Columns', `<p class="b-m v" style="margin-bottom:12px">Choose what the table shows. Click a column heading to sort by it.</p><div id="col-body">${colBody()}</div>`, [{ key: 'done', label: 'Done', cls: 'filled' }]);
A.coltoggle = (el) => {
  const k = el.dataset.k, c = S.lib.cols, i = c.indexOf(k);
  i < 0 ? c.push(k) : c.splice(i, 1);
  if (i >= 0 && S.lib.sort && S.lib.sort.k === k) { S.lib.sort = null; store.set('libsort', null); }
  store.set('libcols', c); $('#col-body').innerHTML = colBody(); renderLib();
};
A.colreset = () => { S.lib.cols = [...DEFAULT_COLS]; store.set('libcols', S.lib.cols); if (S.lib.sort && !colsOn().some((c) => c.id === S.lib.sort.k)) S.lib.sort = null; $('#col-body').innerHTML = colBody(); renderLib(); };
A.selall = (el) => {
  const rows = libCur ? libCur.rows : [];
  rows.forEach((t) => (el.checked ? S.lib.sel.add(t.id) : S.lib.sel.delete(t.id)));
  $$('#lib-tbl [data-act=sel]').forEach((cb) => { cb.checked = S.lib.sel.has(cb.dataset.id); const r = cb.closest('.trow'); if (r) r.classList.toggle('sel', cb.checked); });
  libSelPatch();
};
A.clearsel = () => { S.lib.sel.clear(); renderLib(); };
A.focus = (el) => {
  const prev = S.lib.focus; S.lib.focus = el.dataset.id;
  if (window.innerWidth < 1100) {
    const t = trackOf(S.lib.focus);
    if (t) { dialog('Song details', `<div class="col" style="gap:16px">${detailHTML(t, true)}</div>`, [{ key: 'close', label: 'Close' }]).then(() => clearTint($('#dlg'))); tintEl($('#dlg'), t); }
    return;
  }
  if (S.route !== 'library' || !$('#lib-aside')) return render();
  $$('#lib-tbl .trow.focus').forEach((r) => r.classList.remove('focus'));
  const row = el.closest('.trow'); if (row) row.classList.add('focus');
  if (prev !== S.lib.focus) { $('#lib-aside').innerHTML = libAsideHTML(); tintDetail(); }
};
A.libreload = async () => {
  try { await api('/api/library/rescan', { method: 'POST' }); snack('Rescanning your library…'); refreshTasks(); } catch (e) { oops('Couldn’t start a rescan')(e); }
};
A.enrich = (el) => enrichPaths(idsOf(el));
A.enrichsel = () => enrichPaths([...S.lib.sel]);
async function renamePaths(paths) {
  try {
    const d = await api('/api/library/mistag/rename', { method: 'POST', body: { files: paths } });
    const map = d.renamed || {};
    for (const [from, to] of Object.entries(map)) {
      const t = trackOf(from); if (!t || from === to) continue;
      t.path = to; t.id = to; t.file = baseName(to);
      if (S.lib.sel.delete(from)) S.lib.sel.add(to);
    }
    const n = Object.entries(map).filter(([f, t]) => f !== t).length;
    snack(firstError(d) ? `Renamed ${n}. ${firstError(d)}` : `Renamed ${plural(n, 'file')} from their tags`);
    refreshScans(); render(); later();
  } catch (e) { oops('Couldn’t rename')(e); }
}
A.renamesel = () => renamePaths([...S.lib.sel]);
A.movesel = async () => {
  const paths = [...S.lib.sel];
  const k = await dialog(`Move ${plural(paths.length, 'file')}`, `<div class="field" style="margin-top:8px"><div class="box"><label for="dlg-input">Destination folder</label><input id="dlg-input" placeholder="e.g. Artist/Album" autocomplete="off"></div><span class="help">Relative to the library root. Created if it doesn’t exist.</span></div>`,
    [{ key: 'no', label: 'Cancel' }, { key: 'yes', label: 'Move', cls: 'filled' }]);
  const dest = ($('#dlg-input')?.value || '').trim().replace(/^\/+|\/+$/g, '');
  if (k !== 'yes' || !dest) return;
  let moved = 0, err = '';
  for (const p of paths) {
    try {
      await api('/api/library/move', { method: 'POST', body: { path: p, dest } });
      const t = trackOf(p); if (t) { t.dir = dest; t.path = `${dest}/${t.file}`; t.id = t.path; }
      S.lib.sel.delete(p); moved++;
    } catch (e) { err = e.message; }
  }
  snack(err ? `Moved ${moved} of ${paths.length}. ${err}` : `Moved ${plural(moved, 'file')} to ${dest}`);
  refreshScans(); render(); later();
};
A.dlsel = async () => {
  const paths = [...S.lib.sel];
  if (paths.length > 5 && !(await confirmDlg(`Download ${paths.length} files?`, 'Your browser will save them one after another.', 'Download', false))) return;
  for (const p of paths) { downloadFile(`/api/library/download?path=${encodeURIComponent(p)}`); await sleep(500); }
};
A.dlfiles = (el) => downloadFile(`/api/library/download?path=${encodeURIComponent(el.dataset.id)}`);
A.delsel = async () => {
  const paths = [...S.lib.sel];
  if (!(await confirmDlg(`Delete ${plural(paths.length, 'file')}?`, 'The files are removed from disk. This can’t be undone.', 'Delete'))) return;
  let gone = 0, err = '';
  for (const p of paths) {
    try { await api(`/api/library/file?path=${encodeURIComponent(p)}`, { method: 'DELETE' }); gone++; S.lib.tracks = S.lib.tracks.filter((t) => t.path !== p); S.lib.sel.delete(p); if (S.lib.focus === p) S.lib.focus = null; } catch (e) { err = e.message; }
  }
  rebuildDirCovers();
  snack(err ? `Deleted ${gone} of ${paths.length}. ${err}` : `Deleted ${plural(gone, 'file')}`);
  refreshScans(); render(); later();
};

// Health
A.htab = (el) => { S.h.tab = el.dataset.k; render(); };
A.hfilter = (el) => { const f = S.h.filters; f.has(el.dataset.k) ? f.delete(el.dataset.k) : f.add(el.dataset.k); render(); };
A.keep = (el) => { S.h.keep[el.dataset.g] = +el.dataset.i; render(); };
A.keepall = (el) => { const g = el.dataset.g; if (S.h.keep[g] === 'all') delete S.h.keep[g]; else S.h.keep[g] = 'all'; render(); };
A.skipall = () => { dupGroups().forEach((g) => { S.h.keep[g.id] = 'all'; }); render(); };
A.applydups = async () => {
  const plan = dupPlan();
  if (!(await confirmDlg(`Remove ${plural(plan.files, 'file')}?`, `This frees ${fmtMB(plan.mb)}. The removed files are deleted from disk.`, 'Remove files'))) return;
  const groups = dupGroups().filter((g) => keepOf(g) !== 'all').map((g) => ({ keep: g.files[keepOf(g)].path, remove: g.files.filter((_, i) => i !== keepOf(g)).map((f) => f.path) }));
  try {
    const d = await api('/api/library/duplicates/apply', { method: 'POST', body: { groups } });
    snack(d.errors && d.errors.length ? `Removed ${d.removed}. ${d.errors[0]}` : `Removed ${plural(d.removed, 'file')} · ${fmtMB((d.freed || 0) / MB)} freed`);
    refreshScans(); later();
  } catch (e) { oops('Couldn’t remove the files')(e); }
};
A.scan = async () => {
  try {
    const d = await api('/api/library/scan/library', { method: 'POST' });
    snack(d.started === false ? 'A scan is already running' : 'Scanning your library for duplicates and mistagged songs…');
    refreshTasks();
  } catch (e) { oops('Couldn’t start the scan')(e); }
};
A.hsel = (el) => { const s = S.h.sel[el.dataset.set]; const id = el.dataset.id; el.checked ? s.add(id) : s.delete(id); render(); };
A.hselall = (el) => {
  const set = el.dataset.set; const list = set === 'name' ? misnamed() : missingList();
  list.forEach((t) => (el.checked ? S.h.sel[set].add(t.id) : S.h.sel[set].delete(t.id))); render();
};
A.repair = async () => {
  const files = [...S.h.sel.mis];
  try {
    const d = await api('/api/library/repair', { method: 'POST', body: { files } });
    snack(d.errors && d.errors.length ? `Queued ${d.queued}. ${d.errors[0]}` : `Re-downloading ${plural(d.queued, 'song')} from their tags`, { label: 'View queue', fn: () => go('download') });
    S.h.sel.mis.clear(); refreshScans(); refreshJobs(); later();
  } catch (e) { oops('Couldn’t repair')(e); }
};
A.hdelete = async () => {
  const files = [...S.h.sel.mis];
  if (!(await confirmDlg(`Delete ${plural(files.length, 'file')}?`, 'The files are removed from disk. This can’t be undone.', 'Delete'))) return;
  try {
    const d = await api('/api/library/mistag/delete', { method: 'POST', body: { files } });
    snack(firstError(d) ? `Deleted ${(d.removed || []).length}. ${firstError(d)}` : `Deleted ${plural((d.removed || []).length, 'file')}`);
    S.h.sel.mis.clear(); refreshScans(); later();
  } catch (e) { oops('Couldn’t delete')(e); }
};
A.renamenames = () => {
  const sel = S.h.sel.name; const paths = (sel.size ? [...sel] : misnamed().map((t) => t.id));
  sel.clear(); renamePaths(paths);
};
A.enrichmiss = () => { const sel = S.h.sel.miss; const paths = sel.size ? [...sel] : missingList().map((t) => t.id); sel.clear(); enrichPaths(paths); };

// Settings
A.section = (el) => { S.section = el.dataset.k; render(); };
A.scheme = (el) => { store.set('scheme', el.dataset.k); applyTheme(); render(); };
A.mode = (el) => { store.set('mode', el.dataset.k); applyTheme(); render(); };
A.theme = () => { store.set('mode', document.documentElement.dataset.mode === 'dark' ? 'light' : 'dark'); applyTheme(); render(); };
A.tog = (el) => { const p = el.dataset.path; setPath(S.set, p, !getPath(S.set, p)); saved(); render(); };
A.m3u = (el) => { S.set.m3u = el.dataset.k; saved(); render(); };
A.token = (el) => { S.set.fmt += el.dataset.t; const i = $('#f-fmt'); if (i) i.value = S.set.fmt; const pv = $('#fmt-preview'); if (pv) pv.textContent = fmtPreview(); saved(); };
A.srcon = (el) => { const s = S.set.sources.find((x) => x.id === el.dataset.id); s.on = !s.on; saved(); render(); };
A.srcmove = (el) => {
  const a = S.set.sources, i = a.findIndex((x) => x.id === el.dataset.id), j = i + +el.dataset.d;
  if (j < 0 || j >= a.length) return;
  [a[i], a[j]] = [a[j], a[i]]; saved(); render();
  $(`[data-act=srcmove][data-id="${el.dataset.id}"][data-d="${el.dataset.d}"]`)?.focus();
};
A.day = (el) => { const d = +el.dataset.d, days = S.set.lb.days; const i = days.indexOf(d); i < 0 ? days.push(d) : days.splice(i, 1); days.sort(); saved(); render(); };
A.extrefresh = async () => {
  snack('Installing extensions…');
  try { await api('/api/extensions/refresh', { method: 'POST' }); snack('Extensions refreshed'); loadExt(); } catch (e) { oops('Extension install failed')(e); }
};
A.tidalrefresh = async () => {
  snack('Refreshing Tidal APIs…');
  try { const d = await api('/api/tidal/refresh', { method: 'POST' }); snack(`Tidal APIs refreshed · ${plural(d.count || 0, 'endpoint')}`); loadExt(); } catch (e) { oops('Tidal refresh failed')(e); }
};
A.resetstats = async () => { try { await api('/api/providers', { method: 'DELETE' }); snack('Provider stats reset'); loadExt(); } catch (e) { oops('Couldn’t reset')(e); } };
A.reindex = A.libreload;
A.lbsync = async () => {
  try { await api('/api/listenbrainz/sync', { method: 'POST' }); snack('Syncing ListenBrainz recommendations…'); refreshTasks(); } catch (e) { oops('Couldn’t sync')(e); }
};
// Background tasks: everything /api/tasks reports, live while the dialog is open.
const STOPPABLE = { 'scan-library': ['/api/library/scan/library', 'Scan'], 'scan-dups': ['/api/library/scan/dups', 'Scan'], 'scan-mistag': ['/api/library/scan/mistag', 'Scan'], 'lib-enrich': ['/api/library/enrich', 'Enrichment'] };
function tasksBody() {
  const list = S.tasks.slice().sort((x, y) => Number(y.running) - Number(x.running));
  if (!list.length) return emptyState('task_alt', 'No background tasks');
  return `<div class="col" style="gap:8px">${list.map((t) => {
    const pct = t.running && t.progress_total ? Math.min(100, (t.progress_done / t.progress_total) * 100) : null;
    return `<div class="card ${t.running ? '' : 'idle'}" style="padding:12px 14px">
      <div class="row" style="gap:12px"><span class="ms ${t.running ? 'spin' : ''}" aria-hidden="true" style="color:${t.running ? 'var(--md-primary)' : 'var(--md-outline)'}">${t.running ? 'progress_activity' : 'check_circle'}</span>
        <div class="grow col"><span class="t-s ell">${esc(t.label)}</span><span class="b-m v" style="white-space:normal">${esc(t.detail || (t.running ? 'Working…' : 'Idle'))}</span></div>
        ${t.running && pct != null ? `<span class="mono l-m">${Math.round(pct)}%</span>` : ''}
        ${t.running && STOPPABLE[t.id] ? `<button class="btn text danger" data-act="taskstop" data-id="${t.id}" style="height:32px">Stop</button>` : ''}</div>
      ${t.running ? `<div class="lp ${pct == null ? 'ind' : ''}" role="progressbar" aria-label="${esc(t.label)}" ${pct != null ? `aria-valuenow="${Math.round(pct)}" aria-valuemin="0" aria-valuemax="100"` : ''}><span class="a" style="width:${pct == null ? 30 : pct}%"></span><span class="t"></span></div>` : ''}</div>`;
  }).join('')}</div>`;
}
let tasksPoll;
A.tasks = () => {
  clearInterval(tasksPoll);
  tasksPoll = setInterval(refreshTasks, 1000);   // faster than the usual 3 s while someone is watching
  dialog('Background tasks', `<div id="tasks-body">${tasksBody()}</div>`, [{ key: 'close', label: 'Close', cls: 'tonal' }]).then(() => clearInterval(tasksPoll));
};
A.taskstop = async (el) => {
  const [path, what] = STOPPABLE[el.dataset.id] || [];
  if (!path) return;
  try { await api(path, { method: 'DELETE' }); snack(`${what} stopping…`); refreshTasks(); } catch (e) { oops('Couldn’t stop it')(e); }
};
// Read-only VPN details: nothing here changes the connection.
A.vpn = async () => {
  const v = S.vpn;
  const row = (icon, k, val) => `<div class="row" style="min-height:48px;gap:16px">${ic(icon, 'v')}<span class="b-m v" style="width:96px">${k}</span><span class="b-l">${val}</span></div>`;
  const draw = (ip) => {
    const loc = ip && !ip.error ? [ip.city, ip.regionName, ip.country].filter(Boolean).join(', ') : '';
    return `<div style="margin-bottom:8px">${v.on ? '<span class="tag vpn-on">' + ic('check') + 'Connected</span>' : '<span class="tag vpn-off">' + ic('close') + 'Not connected</span>'}</div>
      ${row('schedule', 'Uptime', v.on && v.since ? span(Date.now() / 1000 - v.since) : '—')}
      ${row('public', 'IP address', ip && !ip.error ? `<span class="mono" style="font-size:15px">${esc(ip.query)}</span>` : ip ? '—' : 'Looking up…')}
      ${row('location_on', 'Location', ip && !ip.error ? esc(loc || '—') : ip ? '—' : 'Looking up…')}
      ${row('dns', 'Provider', ip && !ip.error && ip.isp ? esc(ip.isp) : ip ? '—' : 'Looking up…')}
      ${!v.on && v.known ? '<p class="b-m" style="margin-top:8px">Downloads go out over your normal connection.</p>' : ''}`;
  };
  dialog('VPN', `<div id="vpn-body">${draw(null)}</div>`, [{ key: 'close', label: 'Close', cls: 'tonal' }]);
  let ip = {};
  try { ip = await api('/api/ip'); } catch { ip = { error: true }; }
  const b = $('#vpn-body'); if (b) b.innerHTML = draw(ip);
};

// ── Loading data ─────────────────────────────────────────────────────────
// The server sends bytes and raw tag strings; the views want MB, flags and zero-padded track numbers.
function adaptTrack(r) {
  return {
    id: r.path, path: r.path, dir: r.dir, file: r.file, title: r.title, artist: r.artist, album: r.album,
    year: r.year || '', no: r.no != null ? String(r.no).padStart(2, '0') : '', fmt: r.fmt, kbps: r.kbps, lossless: r.lossless, len: r.len,
    size: r.size / MB, genre: !!r.genre, genreName: r.genre, mbid: !!r.mbid, bpm: !!r.bpm, bpmVal: r.bpm, cover: r.cover,
    isrc: r.isrc, addedDays: Math.max(0, Math.floor((Date.now() / 1000 - r.mtime) / 86400)), expected: r.expected,
    _hay: `${r.title} ${r.artist} ${r.album} ${r.year || ''}`.toLowerCase(),   // search text, built once
  };
}
let tracksTimer;
// New track data: on the library page redraw only the table and details (the search box stays put).
function tracksChanged(shell) {
  if (S.route === 'library' && $('#lib-body') && !shell) {
    renderLib({ aside: true });
    const scope = libScope(); const tot = $('#lib-total');
    if (tot) tot.textContent = `· ${plural(scope.length, 'track')} · ${fmtMB(scope.reduce((x, t) => x + t.size, 0))}`;
  } else if (S.route === 'library' || S.route === 'health') render();
  else renderChrome();
}
async function loadTracks(quiet) {
  clearTimeout(tracksTimer);
  try {
    const d = await api('/api/library/tracks');
    const L = S.lib;
    L.ready = d.ready; L.pending = d.pending || 0; L.error = '';
    L.tracks = (d.tracks || []).map(adaptTrack); L.loaded = true;
    const have = new Set(L.tracks.map((t) => t.path));
    for (const p of [...L.sel]) if (!have.has(p)) L.sel.delete(p);
    for (const k of Object.keys(S.h.sel)) for (const p of [...S.h.sel[k]]) if (!have.has(p) && k !== 'mis') S.h.sel[k].delete(p);
    if (L.focus && !have.has(L.focus)) L.focus = null;
    const lost = L.path.length && L.tracks.length && !L.tracks.some((t) => t.dir === L.path.join('/') || t.dir.startsWith(L.path.join('/') + '/'));
    if (lost) L.path = [];
    rebuildDirCovers();
    tracksChanged(lost);
    if (!d.ready || d.pending) tracksTimer = setTimeout(() => loadTracks(true), 2500);
  } catch (e) {
    S.lib.error = e.message; S.lib.loaded = true; render();
    tracksTimer = setTimeout(() => loadTracks(true), 8000);
  }
}
async function loadSettings() {
  try {
    const cfg = await api('/api/settings');
    S.set = fromServer(cfg); S.setLoaded = true; S.setError = '';
    if (cfg.quality) { S.quality = cfg.quality; store.set('quality', S.quality); }
    render();
  } catch (e) { S.setError = e.message; snack(`Couldn’t load settings: ${e.message}`); }
}
async function loadExt() {
  try { S.ext = await api('/api/extensions/status'); } catch { /* shown as "checking" */ }
  try { S.providers = (await api('/api/providers')).providers || []; } catch { /* no stats yet */ }
  if (S.route === 'settings') render();
}
async function loadVpn() {
  try {
    const d = await api('/api/vpn');
    const next = { known: true, on: !!d.connected, since: d.connected_since || null };
    const changed = !S.vpn.known || S.vpn.on !== next.on;
    S.vpn = next;
    if (changed) renderChrome();
  } catch { /* keep the last known state */ }
}
async function loadVersion() { try { S.ver = await api('/api/spotiflac/version'); } catch { /* optional */ } }
function enterRoute() {
  if ((S.route === 'library' || S.route === 'health') && !S.lib.loaded) loadTracks();
  if (S.route === 'health') {
    if (S.h.dups === null && !S.h.dupsInfo) loadScan('dups');
    if (S.h.mis === null && !S.h.misInfo) loadScan('mistag');
  }
  if (S.route === 'settings') { loadExt(); loadVersion(); }
}
function boot() {
  loadSettings(); refreshJobs(); refreshTasks(); loadVpn();
  setInterval(() => { if (!document.hidden) refreshJobs(); }, 2500);
  setInterval(() => { if (!document.hidden) refreshTasks(); }, 3000);
  setInterval(() => { if (!document.hidden) loadVpn(); }, 10000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { refreshJobs(); refreshTasks(); loadVpn(); } });
}

// ── Inputs (typing never re-renders settings fields, so focus and caret stay put) ──
let lqTimer;
const INPUT = {
  seed: (el) => { store.set('seed', el.value); store.set('scheme', 'custom'); applyTheme(); },
  q: (el) => { S.q = el.value; scheduleSearch(); renderResults(); },
  lq: (el) => { S.lib.q = el.value; clearTimeout(lqTimer); lqTimer = setTimeout(() => renderLib(), S.lib.tracks.length > 2000 ? 160 : 80); },
  text: (el) => { setPath(S.set, el.dataset.key, el.value); if (el.dataset.key === 'fmt') { const pv = $('#fmt-preview'); if (pv) pv.textContent = fmtPreview(); } saved(); },
  num: (el) => { setPath(S.set, el.dataset.key, Math.max(0, Number(el.value) || 0)); saved(); },
  slider: (el) => {
    const { key, min, max } = el.dataset; const v = +el.value; const pct = ((v - +min) / (+max - +min)) * 100;
    S.set[key] = v; el.style.setProperty('--pct', pct + '%');
    const o = $('#out-' + key); if (o) { o.textContent = v; o.style.left = `calc(${pct}% + ${((0.5 - pct / 100) * 4).toFixed(2)}px)`; }
    saved();
  },
};

// ── Wiring ───────────────────────────────────────────────────────────────
document.addEventListener('click', (e) => {
  if (e.target.closest('[data-snack-act]')) { const fn = snackAction; $('#snack').classList.remove('show'); if (fn) fn(); return; }
  const el = e.target.closest('[data-act]');
  if (el && !el.disabled && A[el.dataset.act]) A[el.dataset.act](el, e);
});
document.addEventListener('change', (e) => { if (e.target.dataset && e.target.dataset.change === 'seed') render(); });
document.addEventListener('input', (e) => { const f = e.target.dataset && e.target.dataset.input; if (f && INPUT[f]) INPUT[f](e.target); });
document.addEventListener('keydown', (e) => {
  if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) && !$('#dlg').open) { e.preventDefault(); if (S.route !== 'download') go('download'); setTimeout(() => $('#q')?.focus(), 0); }
});
// Drag-to-reorder for the source list
let dragId = null;
document.addEventListener('dragstart', (e) => { const r = e.target.closest && e.target.closest('[data-src]'); if (!r) return; dragId = r.dataset.src; r.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', dragId); });
document.addEventListener('dragover', (e) => { if (dragId && e.target.closest('[data-src]')) e.preventDefault(); });
document.addEventListener('drop', (e) => {
  const r = e.target.closest && e.target.closest('[data-src]'); if (!r || !dragId) return;
  e.preventDefault();
  const a = S.set.sources, from = a.findIndex((x) => x.id === dragId), to = a.findIndex((x) => x.id === r.dataset.src);
  if (from !== to) { a.splice(to, 0, a.splice(from, 1)[0]); saved(); }
  dragId = null; render();
});
document.addEventListener('dragend', () => { dragId = null; $$('.dragging').forEach((n) => n.classList.remove('dragging')); });
matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => { if (store.get('mode', 'auto') === 'auto') { applyTheme(); render(); } });
window.addEventListener('hashchange', onRoute);
window.addEventListener('resize', (() => { let t; return () => { clearTimeout(t); t = setTimeout(() => { if (S.route === 'library') render(); }, 150); }; })());

applyTheme();
import(`${APP.assets}mcu.js?v=1`).then((m) => { MCU = m; applyTheme(); render(); }).catch(() => { /* static fallback colours stay */ });
onRoute();
boot();
