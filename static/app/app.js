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
async function api(path, { method = 'GET', body, timeout = method === 'GET' ? 60000 : 300000 } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method, credentials: 'same-origin',
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeout),
    });
  } catch (e) {
    throw new Error(e && e.name === 'TimeoutError' ? 'The server didn’t answer in time' : 'Couldn’t reach the server');
  }
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
  log: ['vpn', 'downloads', 'enrich', 'system'],
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
    log: c.log_categories || DEFAULT_SET.log,
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
    listenbrainz_enabled: s.lb.on, listenbrainz_username: s.lb.user, listenbrainz_days: s.lb.days, listenbrainz_time: s.lb.time, log_categories: s.log,
    quality: S.quality,
  };
}
const S = {
  route: 'download',
  // download
  q: '', type: 'all', results: [], resultsFor: '', hasMore: false, nextOffset: 0, searching: false, searchError: '', inLib: {},
  // exact artist matches above the results: their discography, open state and per-artist selection, all keyed by URL
  artists: [], art: { open: new Set(), rel: {}, albOpen: new Set(), trk: {}, sel: {} },
  quality: store.get('quality', 'lossless'),
  jobs: [], jobsLoaded: false, doneAll: false, qsel: null, // qsel: Set of picked job ids while selecting, else null
  vpn: { known: false, on: false, since: null, ip: null },
  tasks: [],
  // library
  lib: { filtersOpen: false, path: [], view: 'tracks', chips: new Set(), q: '', sel: new Set(), focus: null, tracks: [], loaded: false, ready: true, pending: 0, error: '', page: 1, focusT: null,
    pg: { loaded: false, ready: true, pending: 0, error: '', rows: [], folders: [], count: 0, total: 0, page: 1, pages: 1, scopeN: 0, scopeSize: 0 } },
  // health: null = no scan result yet
  h: {
    tab: 'dups', filters: new Set(['id', 'tags']),
    dups: null, dupsInfo: null, keep: {}, mis: null, misInfo: null,
    sel: { mis: new Set(), name: new Set(), miss: new Set(), bad: new Set() },
    org: { fmt: '{artist}/{album}/{track} {title}', phase: 'idle', done: 0, total: 0, moved: 0, errors: 0, ops: [], msg: '' },
  },
  disc: { data: null, error: '', basis: new Set(['artists', 'genres', 'similar']), recent: false, hiddenOpen: false, busy: false },
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
// Songs that can't play: the file is empty or cut short, has no audio in it, or can't be opened at all.
const brokenWhy = (t) => (t.bytes === 0 ? 'Empty file' : t.unreadable ? 'Can’t be read' : !t.len ? 'No audio (0:00)' : t.bytes < 8192 ? 'Almost empty' : '');
const brokenList = () => S.lib.tracks.filter((t) => brokenWhy(t));
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
  ['discover', 'Discover', 'explore'],
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
    ${S.route === 'download' || S.route === 'discover' || S.route === 'health' || (S.route === 'library' && S.lib.sel.size) ? '' : `<button class="fab ext mfab" data-act="paste">${ic('add_link')}<span class="l-l">Paste link</span></button>`}`;
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
// Library covers are read off the music folder, and a page sorted by artist asks for one per row. Fetched all at once they
// fill the browser's few connections and every other request (search, song details) waits behind them, so they load
// through a small queue: a few at a time, top rows first, one retry if the server says it is busy.
const coverImg = (url) => (!url ? '' : url.startsWith('/api/library/cover') ? `<img data-csrc="${esc(url)}" alt="">` : `<img src="${esc(url)}" alt="" loading="lazy" onerror="this.remove()">`);
const coverQ = [], coverLoading = new Set(); let coverBusy = 0;
function pumpCovers() {
  while (coverBusy < 3 && coverQ.length) {
    const img = coverQ.shift();
    if (!img.isConnected || img.getAttribute('src')) continue;
    coverBusy++;
    let freed = false;
    const free = () => { if (freed) return; freed = true; clearTimeout(guard); coverLoading.delete(img); coverBusy--; pumpCovers(); };
    img._free = free; coverLoading.add(img);
    const guard = setTimeout(free, 20000);      // a stalled request must not hold its slot for good
    img.addEventListener('load', free, { once: true });
    img.addEventListener('error', () => {
      if (!img.dataset.retried && img.isConnected) { img.dataset.retried = '1'; img.removeAttribute('src'); setTimeout(() => { coverQ.push(img); pumpCovers(); }, 1500); } else img.remove();
      free();
    }, { once: true });
    img.src = img.dataset.csrc;
  }
}
function queueCovers() {
  for (const img of coverLoading) if (!img.isConnected) img._free();      // the table was redrawn: those rows are gone
  document.querySelectorAll('img[data-csrc]:not([src]):not([data-queued])').forEach((img) => { img.dataset.queued = '1'; coverQ.push(img); });
  pumpCovers();
}
new MutationObserver(() => requestAnimationFrame(queueCovers)).observe(document.documentElement, { childList: true, subtree: true });
function coverTile(d, size = 48) {
  return `<span class="coverthumb" style="width:${size}px;height:${size}px;background:${coverGradient(d.title)}" aria-hidden="true">${ic(TYPE_ICON[d.kind] || 'album', '', `font-size:${Math.round(size / 2.2)}px`)}${coverImg(d.url)}</span>`;
}
// Library rows always show a cover. Tracks in one folder share art, so one request per folder serves them all.
const dirCover = new Map();
function rebuildDirCovers() {
  dirCover.clear();
  for (const t of S.lib.tracks) if (t.cover && !dirCover.has(t.dir)) dirCover.set(t.dir, t);
}
// Thumbnail sized for where it's shown; v= is the file's version, so the browser may keep it indefinitely.
const coverUrl = (t, size = 96) => {
  if (!t.cover) return '';
  const src = t.csrc || dirCover.get(t.dir) || t;
  return `/api/library/cover?path=${encodeURIComponent(src.path)}&s=${size}&v=${src.mtime || 0}`;
};
const libCover = (t) => (t.cover ? coverTile({ title: t.album || t.title, kind: 'track', url: coverUrl(t) }, 40)
  : `<span class="coverthumb" style="width:40px;height:40px;background:var(--md-sc-highest);color:var(--md-outline)" role="img" aria-label="No cover art">${ic('image_not_supported', '', 'font-size:20px')}</span>`);

// ── Search ───────────────────────────────────────────────────────────────
let searchTimer, searchSeq = 0;
function scheduleSearch() {
  clearTimeout(searchTimer);
  const q = S.q.trim();
  if (!q || LINK_RE.test(q)) { searchSeq++; S.results = []; S.artists = []; S.resultsFor = ''; S.searching = false; S.searchError = ''; return; }
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
    if (!offset) S.artists = d.artist_match || [];
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

// Pasting nothing but Spotify track links queues them right away and leaves the box empty.
// Albums, playlists and artists still wait for a click, since one paste can mean hundreds of songs.
const TRACK_URL = /^https?:\/\/open\.spotify\.com\/(?:intl-[a-z-]+\/)?track\/[A-Za-z0-9]+(?:[?#]\S*)?$/;
function pastedTracks(text) {
  const lines = String(text || '').split(/[\s,]+/).filter(Boolean);
  return lines.length && lines.every((l) => TRACK_URL.test(l)) ? [...new Set(lines.map((l) => l.replace(/[?#].*$/, '')))] : null;
}
async function quickQueue(urls) {
  S.q = ''; scheduleSearch();
  const box = $('#q'); if (box) box.value = '';
  renderResults();
  try {
    await api('/api/download', { method: 'POST', body: { urls: urls.join('\n'), quality: S.quality } });
    snack(urls.length > 1 ? `Added ${plural(urls.length, 'song')} to the queue` : 'Added to the queue');
    refreshJobs();
  } catch (e) { oops('Couldn’t queue it')(e); }
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
  const cards = S.type === 'all' || S.type === 'artist' ? S.artists : [];
  const carded = new Set(S.artists.map((a) => a.url));
  const shown = S.results.map((r, i) => [r, i]).filter(([r]) => (S.type === 'all' || r.type === S.type) && !carded.has(r.url));
  if (!shown.length && !cards.length) return emptyState('search_off', 'No matches', `Nothing for “${S.q}”. Try fewer words or paste a Spotify link.`);
  return cards.map(artistCard).join('') + shown.map(([r, i]) => {
    const st = S.inLib[r.url];
    return `<div class="li">
      <div class="lead">${ic(TYPE_ICON[r.type])}${coverImg(r.cover_url)}</div>
      <div class="grow col"><span class="b-l ell">${esc(r.title)}</span><span class="b-m v ell">${cap(r.type)}${r.subtitle ? ' · ' + esc(r.subtitle) : ''}${r.year ? ' · ' + r.year : ''}${r.track_count ? ' · ' + plural(r.track_count, 'track') : ''}</span></div>
      ${st === 'full' ? `<span class="tag primary hide-sm">${ic('check')}In library</span>` : st === 'partial' ? `<span class="tag neutral hide-sm">Partly in library</span>` : ''}
      <span class="mono l-m v hide-sm" style="width:36px;text-align:right">${r.duration_ms ? fmtLen(Math.round(r.duration_ms / 1000)) : ''}</span>
      <button class="ib ${st === 'full' ? 'outlined' : 'filled'}" data-act="dl" data-i="${i}" aria-label="Download ${esc(r.title)}">${ic('download')}</button></div>`;
  }).join('') + (S.hasMore ? `<div class="row" style="justify-content:center;padding:8px"><button class="btn tonal" data-act="more" ${S.searching ? 'disabled' : ''}>${S.searching ? 'Loading…' : 'Load more'}</button></div>` : '');
}

// ── Artist card: an exact artist match opens into albums and singles, albums open into tracks ──
// Selection per artist: rel = whole releases, trk = release URL → the tracks picked from a release that isn't whole.
const artSel = (a) => (S.art.sel[a] ||= { rel: new Set(), trk: new Map() });
const relsOf = (a) => (S.art.rel[a] && S.art.rel[a].list) || [];
const relState = (sel, r) => (sel.rel.has(r) ? 'all' : sel.trk.has(r) ? 'some' : 'none');
function artState(a) {
  const sel = artSel(a), rels = relsOf(a);
  if (rels.length && rels.every((r) => sel.rel.has(r.url))) return 'all';
  return sel.rel.size || sel.trk.size ? 'some' : 'none';
}
function artPicked(a) {
  const sel = artSel(a), rels = relsOf(a);
  let songs = 0;
  for (const r of rels) if (sel.rel.has(r.url)) songs += r.track_count || 0;
  for (const s of sel.trk.values()) songs += s.size;
  return { jobs: sel.rel.size + [...sel.trk.values()].reduce((n, s) => n + s.size, 0), songs };
}
// One checkbox; data-fk lets a redraw put focus back on it.
const triBox = (act, st, label, data, wrap = 'label') => `<${wrap} class="cb"><input type="checkbox" data-act="${act}" ${data} data-fk="${esc(act + '|' + data)}" ${st === 'all' ? 'checked' : ''} ${st === 'some' ? 'data-ind="1"' : ''} aria-label="${esc(label)}"></${wrap}>`;
const libTag = (st, what = 'In library') => (st === 'full' ? `<span class="tag primary" title="${what}">${ic('check')}<span class="hide-sm">${what}</span></span>`
  : st === 'partial' ? `<span class="tag neutral" title="Partly in library">${ic('incomplete_circle')}<span class="hide-sm">Partly in library</span></span>` : '');
const chevron = (open) => ic('expand_more', 'art-chev', open ? 'transform:rotate(180deg)' : '');

function artistCard(a) {
  const open = S.art.open.has(a.url), d = S.art.rel[a.url], rels = relsOf(a.url);
  const nAlb = rels.filter((r) => r.type === 'album').length, nSgl = rels.length - nAlb;
  const have = rels.filter((r) => S.inLib[r.url] === 'full').length;
  const picked = artPicked(a.url), du = `data-a="${esc(a.url)}"`;
  const sub = ['Artist', a.subtitle, d && d.list ? `${plural(nAlb, 'album')} · ${plural(nSgl, 'single')}` : ''].filter(Boolean).map(esc).join(' · ');
  let body = '';
  if (open) {
    if (!d || d.loading) body = Array.from({ length: 3 }, () => `<div class="li art-row"><span class="cb"></span><span class="skel" style="width:48px;height:48px;border-radius:10px"></span><span class="skel grow" style="height:14px;border-radius:4px"></span></div>`).join('');
    else if (d.error) body = `<div class="row" style="padding:8px 16px;gap:12px"><span class="b-m v grow">${esc(d.error)}</span><button class="btn text" data-act="artload" ${du}>Try again</button></div>`;
    else if (!rels.length) body = `<p class="b-m v" style="padding:8px 16px">No albums or singles on Spotify.</p>`;
    else body = [['album', 'Albums'], ['single', 'Singles & EPs']].map(([k, l]) => {
      const list = rels.filter((r) => r.type === k);
      return list.length ? `<h3 class="l-l v art-h">${l}<span class="l-m" style="opacity:.7">${list.length}</span></h3>${list.map((r) => releaseRow(a.url, r)).join('')}` : '';
    }).join('');
  }
  return `<div class="art ${open ? 'open' : ''}">
    <div class="row art-head">${triBox('artsel', artState(a.url), `Select everything by ${a.title}`, du)}
      <button class="art-toggle grow" data-act="artopen" ${du} data-fk="${esc('artopen|' + a.url)}" aria-expanded="${open}">
        <span class="art-ava">${ic('person')}${coverImg(a.cover_url)}</span>
        <span class="col grow" style="min-width:0"><span class="t-m ell">${esc(a.title)}</span><span class="b-m v ell">${sub}</span></span>
        ${have ? `<span class="tag primary hide-sm">${ic('check')}${have} in library</span>` : ''}${chevron(open)}</button></div>
    ${open ? `<div class="art-body">${body}</div>` : ''}
    ${picked.jobs ? `<div class="row art-bar"><span class="b-m grow">${plural(picked.songs, 'song')} selected</span>
      <button class="btn text" data-act="artclear" ${du}>Clear</button><button class="btn filled" data-act="artdl" ${du}>${ic('download')}Download</button></div>` : ''}
  </div>`;
}
function releaseRow(a, r) {
  const sel = artSel(a), open = S.art.albOpen.has(r.url), t = S.art.trk[r.url];
  const du = `data-a="${esc(a)}" data-r="${esc(r.url)}"`;
  let tracks = '';
  if (open) {
    if (!t || t.loading) tracks = `<div class="art-trk"><span class="skel" style="height:12px;width:50%;border-radius:4px;margin:14px 0"></span></div>`;
    else if (t.error) tracks = `<div class="row art-trk"><span class="b-m v grow">${esc(t.error)}</span><button class="btn text" data-act="relload" ${du}>Try again</button></div>`;
    else tracks = t.list.map((x, i) => {
      const on = sel.rel.has(r.url) || (sel.trk.get(r.url) || new Set()).has(x.url);
      return `<label class="row art-trk">${triBox('trksel', on ? 'all' : 'none', `Select ${x.title}`, `${du} data-t="${esc(x.url)}"`, 'span')}
        <span class="mono l-m v" style="width:20px;text-align:right">${x.track_number || i + 1}</span>
        <span class="col grow" style="min-width:0"><span class="b-l ell">${esc(x.title)}</span><span class="b-m v ell">${esc(x.artists)}</span></span>
        ${libTag(x.in_lib ? 'full' : '', 'Downloaded')}<span class="mono l-m v hide-sm" style="width:36px;text-align:right">${x.duration_ms ? fmtLen(Math.round(x.duration_ms / 1000)) : ''}</span></label>`;
    }).join('');
  }
  return `<div class="art-rel">
    <div class="row art-row">${triBox('relsel', relState(sel, r.url), `Select ${r.title}`, du)}
      <button class="art-toggle grow" data-act="relopen" ${du} data-fk="${esc('relopen|' + r.url)}" aria-expanded="${open}">
        ${coverTile({ title: r.title, kind: 'album', url: r.cover_url }, 48)}
        <span class="col grow" style="min-width:0"><span class="b-l ell">${esc(r.title)}</span><span class="b-m v ell">${[r.type === 'album' ? 'Album' : 'Single', r.year, r.track_count ? plural(r.track_count, 'track') : ''].filter(Boolean).join(' · ')}</span></span>
        ${libTag(S.inLib[r.url])}${chevron(open)}</button></div>
    ${open ? `<div class="art-trks">${tracks}</div>` : ''}
  </div>`;
}

async function loadReleases(a) {
  S.art.rel[a] = { loading: true };
  renderResults();
  try { S.art.rel[a] = { list: (await api(`/api/search/artist?url=${encodeURIComponent(a)}`)).releases || [] }; } catch (e) { S.art.rel[a] = { error: e.message }; }
  renderResults();
  verifyReleases(a);
}
// The folder-name guess for an album can be wrong either way, so no badge is shown until its songs have been
// checked one by one (what opening it does). Do that for every release in the background, three at a time.
async function verifyReleases(a) {
  const todo = relsOf(a).map((r) => r.url).filter((u) => !S.art.trk[u]);
  const worker = async () => {
    while (todo.length && S.artists.some((x) => x.url === a)) {
      const u = todo.shift();
      if (!S.art.trk[u]) await loadRelTracks(u);
    }
  };
  await Promise.all([worker(), worker(), worker()]);
}
async function loadRelTracks(r) {
  S.art.trk[r] = { loading: true };
  renderResults();
  try {
    const d = await api(`/api/search/expand?url=${encodeURIComponent(r)}`);
    S.art.trk[r] = { list: d.tracks || [] };
    if (d.in_lib) S.inLib[r] = d.in_lib;   // counted per song now, more exact than the folder guess
  } catch (e) { S.art.trk[r] = { error: e.message }; }
  renderResults();
}
// ── Queue ────────────────────────────────────────────────────────────────
const jobPct = (j) => (j.status === 'running' && j.total ? Math.min(100, ((j.progress || 0) / j.total) * 100) : null);
const jobNow = (j) => ((j.track_results || []).find((t) => t.status === 'downloading') || {}).title;
// A failed job waiting out the retry interval is "queued" with next_retry_at set (and the reason in last_error).
const retryAt = (j) => (j.status === 'queued' && j.next_retry_at ? parseTime(j.next_retry_at) : 0);
const attemptText = (j) => (j.retry_count ? `Retry ${j.retry_count}${j.retry_max ? ' of ' + j.retry_max : ''}` : 'Retry');
function countdown(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s <= 0) return 'now';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`;
}
const retryLine = (j) => { const left = retryAt(j) - Date.now(); return left > 0 ? `${attemptText(j)} in ${countdown(left)}` : `${attemptText(j)} starting…`; };
function jobNote(j) {
  const total = j.total || 0, prog = j.progress || 0, cur = jobNow(j);
  if (j.status === 'running') return total > 1 ? `${cur ? cur + ' · ' : ''}${prog} of ${total} tracks` : (cur || 'Downloading');
  if (retryAt(j)) return j.last_error ? `Failed: ${j.last_error}` : 'Failed, will retry';
  if (j.status === 'queued') return j.retry_count ? `${attemptText(j)} · waiting for a free worker` : 'Waiting for a free worker';
  if (j.status === 'cancelled') return 'Cancelled';
  const mins = j.next_retry_at ? Math.max(0, Math.round((parseTime(j.next_retry_at) - Date.now()) / 60000)) : null;
  return (j.error || 'Failed') + (mins != null && !Number.isNaN(mins) ? ` · retrying in ${mins} min` : '');
}
const jobPick = (j) => (S.qsel ? `<label class="cb"><input type="checkbox" data-act="qpick" data-id="${j.id}" ${S.qsel.has(j.id) ? 'checked' : ''} aria-label="Select ${esc(jobTitle(j))}"></label>` : '');
function jobCard(j) {
  const st = j.status, pct = jobPct(j);
  const meta = st === 'running' ? (pct != null ? `${Math.round(pct)}%` : '') : st === 'queued' ? (j.total ? plural(j.total, 'track') : '')
    : st === 'error' && j.retry_count ? `try ${j.retry_count}${j.retry_max ? '/' + j.retry_max : ''}` : '';
  const ra = retryAt(j);
  const icon = st === 'error' ? 'error' : ra ? 'schedule' : TYPE_ICON[jobKind(j)] || 'music_note';
  const btn = (act, icn, label) => `<button class="ib" data-act="${act}" data-id="${j.id}" aria-label="${label} ${esc(jobTitle(j))}" style="color:inherit">${ic(icn)}</button>`;
  const actions = S.qsel ? '' : st === 'error' || st === 'cancelled' ? btn('jretry', 'refresh', 'Retry') + btn('jremove', 'close', 'Remove')
    : btn('jcancel', 'close', st === 'queued' ? 'Remove' : 'Cancel');
  if (ra) {
    const span = Math.max(1, ra - parseTime(j.finished_at || j.started_at) || 300000);
    return `<div class="card retry" data-retry-job="${j.id}">
    <div class="row" style="gap:14px">${jobPick(j)}<div class="lead">${ic('schedule')}</div>
      <div class="grow col"><span class="t-m ell">${esc(jobTitle(j))}</span><span class="b-m ell" style="opacity:.85" title="${esc(j.last_error || '')}">${esc(jobNote(j))}</span></div>
      <span class="mono l-m" data-retry-at="${ra}" aria-live="off">${esc(retryLine(j))}</span>${btn('jretry', 'refresh', 'Retry now')}${btn('jcancel', 'close', 'Cancel')}</div>
    <div class="lp" aria-hidden="true"><span class="a" data-retry-bar="${ra}" data-retry-span="${span}" style="width:${Math.min(100, Math.max(0, 100 - ((ra - Date.now()) / span) * 100))}%"></span><span class="t"></span></div>
  </div>`;
  }
  return `<div class="card ${st === 'error' ? 'err' : st === 'running' ? '' : 'idle'}">
    <div class="row" style="gap:14px">${jobPick(j)}<div class="lead" style="background:${st === 'error' ? 'var(--md-error)' : ''}">${ic(icon)}${st === 'error' ? '' : coverImg(j.cover_url)}</div>
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
  const fk = document.activeElement && pane.contains(document.activeElement) && document.activeElement.dataset.fk;
  pane.innerHTML = resPaneHTML();
  pane.querySelectorAll('[data-ind]').forEach((cb) => { cb.indeterminate = true; });
  if (fk) pane.querySelector(`[data-fk="${CSS.escape(fk)}"]`)?.focus();
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
function queueBar(q) {
  const n = S.qsel.size, picked = q.filter((j) => S.qsel.has(j.id));
  const live = picked.some((j) => j.status === 'queued' || j.status === 'running'), dead = picked.some((j) => j.status === 'error' || j.status === 'cancelled');
  return `<div class="dock"><span class="b-l grow">${n ? `${n} selected` : 'Select downloads'}</span>
    <button class="btn text" data-act="qall">${n === q.length ? 'None' : 'All'}</button>
    <button class="btn tonal" data-act="qbulk" data-k="retry" ${dead ? '' : 'disabled'}>${ic('refresh')}Retry</button>
    <button class="btn tonal" data-act="qbulk" data-k="cancel" ${live ? '' : 'disabled'}>${ic('stop_circle')}Cancel</button>
    <button class="btn outlined danger" data-act="qbulk" data-k="remove" ${n ? '' : 'disabled'}>${ic('delete')}Remove</button></div>`;
}
function dlSideHTML() {
  const n = (s) => S.jobs.filter((j) => j.status === s).length;
  const failed = n('error');
  const q = queueJobs();
  return `<section class="pane" aria-labelledby="h-q" style="padding:20px;display:flex;flex-direction:column;gap:12px">
      <div class="row"><h2 id="h-q" class="t-l grow">Queue</h2>
        ${failed && !S.qsel ? `<button class="btn text" data-act="retryall">Retry failed</button>` : ''}
        ${q.length ? `<button class="btn text" data-act="qselect">${S.qsel ? 'Done' : 'Select'}</button>` : ''}</div>
      <div class="row" style="gap:8px"><span class="tag tertiary">${n('running')} running</span><span class="tag neutral">${n('queued')} queued</span>${failed ? `<span class="tag error">${failed} failed</span>` : ''}</div>
      ${!S.jobsLoaded ? '<div class="card idle"><span class="skel" style="height:16px;width:60%;border-radius:4px"></span></div>' : q.length ? q.map(jobCard).join('') : emptyState('done_all', 'Queue is empty', 'Search for something to download.')}
      ${S.qsel && q.length ? queueBar(q) : ''}
    </section>
    ${finishedCard()}`;
}

// ── Jobs: polled from the server ─────────────────────────────────────────
let jobsSig = '', prevStatus = null;
let jobsBusy = false;
async function refreshJobs() {
  if (jobsBusy) return;
  jobsBusy = true;
  let d;
  try { d = await api('/api/jobs', { timeout: 15000 }); } catch { return; } finally { jobsBusy = false; }
  const sig = JSON.stringify(d.map((j) => [j.id, j.status, j.title, j.total, j.success_count, j.fail_count, j.error, j.next_retry_at, j.last_error, j.cover_url, j.retry_count, j.finished_at]));
  const first = !S.jobsLoaded;
  S.jobs = d; S.jobsLoaded = true;
  if (S.qsel) for (const id of [...S.qsel]) if (!d.some((j) => j.id === id && j.status in { running: 1, queued: 1, error: 1, cancelled: 1 })) S.qsel.delete(id);
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
let tasksBusy = false;
async function refreshTasks() {
  if (tasksBusy) return;
  tasksBusy = true;
  let d;
  try { d = await api('/api/tasks', { timeout: 15000 }); } catch { return; } finally { tasksBusy = false; }
  const tasks = Array.isArray(d) ? d : d.tasks || [];
  const prev = S.tasks; S.tasks = tasks;
  const sig = JSON.stringify(tasks.map((t) => [t.id, t.running, t.label]));
  // A scan that just finished has a result to fetch; enrichment/index changes mean new tag data.
  for (const t of prev) {
    if (!t.running) continue;
    const now = tasks.find((x) => x.id === t.id);
    if (now && now.running) continue;
    if (t.id === 'scan-library') { loadDups(true); loadScan('mistag'); loadTracks(true); }
    else if (t.id === 'scan-dups') loadDups(true);
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

const qualityTag = (t) => (t.lossless ? '<span class="tag tertiary">Lossless</span>' : t.kbps ? `<span class="tag neutral">${t.kbps} kbps</span>` : '');

// ── Library columns ──────────────────────────────────────────────────────
// Each column knows how to render a cell; sorting happens on the server (_TRACK_SORTS in routes.py). Title is always shown; the rest are
// toggled from the cog in the table header and remembered in this browser.
const dash = '<span style="color:var(--md-outline)">—</span>';
const COLS = [
  { id: 'no', label: '#', name: 'Track number', w: '36px', def: true, cell: (t) => `<span class="mono l-m v">${t.no}</span>` },
  { id: 'title', label: 'Title', w: 'minmax(0,2.2fr)', fixed: true, def: true },
  { id: 'artist', label: 'Artist', w: 'minmax(0,1.2fr)', cell: (t) => `<span class="b-m ell">${esc(t.artist)}</span>` },
  { id: 'album', label: 'Album', w: 'minmax(0,1.3fr)', def: true, cell: (t) => `<span class="b-m ell">${esc(t.album)}</span>` },
  { id: 'year', label: 'Year', w: '56px', cell: (t) => (t.year ? `<span class="mono l-m">${t.year}</span>` : dash) },
  { id: 'genre', label: 'Genre', w: '104px', cell: (t) => (t.genre ? `<span class="b-m ell">${t.genreName}</span>` : dash) },
  { id: 'format', label: 'Format', w: '150px', def: true, cell: (t) => `<span class="row" style="gap:8px"><span class="mono l-m">${t.fmt.toUpperCase()}</span>${qualityTag(t)}</span>` },
  { id: 'bpm', label: 'BPM', w: '56px', right: true, cell: (t) => (t.bpm ? `<span class="mono l-m">${t.bpmVal}</span>` : dash) },
  { id: 'tags', label: 'Tags', w: '110px', def: true, cell: (t) => { const m = missingOf(t); return `<span class="tags4" role="img" aria-label="${m.length ? 'Missing ' + m.join(', ') : 'All tags present'}">${TAG_ICONS.map(([k, i]) => `<span class="ms ${t[k] ? 'on' : 'off'}" aria-hidden="true">${i}</span>`).join('')}</span>`; } },
  { id: 'size', label: 'Size', w: '80px', right: true, cell: (t) => `<span class="mono l-m v">${fmtMB(t.size)}</span>` },
  { id: 'len', label: 'Time', w: '56px', right: true, def: true, cell: (t) => `<span class="mono l-m v">${fmtLen(t.len)}</span>` },
  { id: 'added', label: 'Added', w: '88px', cell: (t) => `<span class="b-m v">${t.addedDays === 0 ? 'Today' : t.addedDays + ' d ago'}</span>` },
  { id: 'isrc', label: 'ISRC', w: '132px', cell: (t) => (t.isrc ? `<span class="mono v ell" style="font-size:12px">${esc(t.isrc)}</span>` : dash) },
  { id: 'file', label: 'File name', w: 'minmax(0,1.5fr)', cell: (t) => { const bad = t.expected && t.file !== t.expected; return `<span class="mono ell ${bad ? '' : 'v'}" style="font-size:12px;${bad ? 'color:var(--md-error)' : ''}" title="${esc(t.file)}">${esc(t.file)}</span>`; } },
  { id: 'dir', label: 'Folder', w: 'minmax(0,1.5fr)', cell: (t) => `<span class="mono v ell" style="font-size:12px" title="${esc(t.dir)}">${esc(t.dir)}</span>` },
];
const DEFAULT_COLS = COLS.filter((c) => c.def).map((c) => c.id);
S.lib.cols = store.get('libcols', DEFAULT_COLS);
S.lib.sort = store.get('libsort', null);
const colsOn = () => COLS.filter((c) => c.fixed || S.lib.cols.includes(c.id));
const colTpl = (cols) => ['40px', ...cols.map((c) => c.w)].join(' ');
const colMinW = (cols) => 40 + cols.reduce((n, c) => n + (/^\d+px$/.test(c.w) ? parseInt(c.w, 10) : 150) + 12, 0);

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
    if (c.id === 'title') return `<button class="title" data-act="cd" data-path="${esc(path)}" style="flex-direction:row;align-items:center;gap:12px">${ic('folder', 'f', 'color:var(--md-primary)')}<span class="col" style="min-width:0"><span class="b-l ell">${esc(f.name)}</span><span class="b-m v ell">${f.leaf ? plural(f.tracks, 'track') : plural(f.dirs, 'album') + ' · ' + plural(f.tracks, 'track')} · ${fmtMB(f.size)}</span></span></button>`;
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
    ? `<div class="row" style="gap:16px;align-items:center">${t.cover ? coverTile({ title: t.album || t.title, kind: 'track', url: coverUrl(t, 192) }, 72)
        : `<span class="coverthumb" style="width:72px;height:72px;border-radius:14px;background:var(--md-sc-highest);color:var(--md-outline)" role="img" aria-label="No cover art">${ic('image_not_supported', '', 'font-size:32px')}</span>`}
        <div class="col grow" style="min-width:0"><h3 class="t-m" style="word-break:break-word;color:var(--md-on-surface)">${esc(t.title)}</h3><span class="b-m v">${sub}</span></div></div>`
    : `<div class="cover" style="position:relative;overflow:hidden;${t.cover ? `background:${coverGradient(t.album || t.title)};color:rgba(255,255,255,.8)` : 'background:var(--md-sc-highest);color:var(--md-outline)'}">${ic(t.cover ? 'album' : 'image_not_supported', '', 'font-size:64px')}<span class="l-m">${t.cover ? '' : 'No cover art'}</span>${t.cover ? `<img src="${esc(coverUrl(t, 480))}" alt="Cover art" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover" onerror="this.remove()">` : ''}</div>
    <div class="col"><h2 class="hl-s" style="word-break:break-word">${esc(t.title)}</h2><span class="b-m v">${sub}</span></div>`;
  return `${head}
    ${waveHTML(t)}
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
      <button class="btn outlined grow" data-act="dlfiles" data-id="${esc(t.id)}">${ic('download')}Download</button></div>
    <div class="enr" data-enr="${esc(t.id)}" aria-live="polite">${enrichHTML(t.id)}</div>`;
}

function libEmpty() {
  const L = S.lib.pg;
  if (!L.loaded) return Array.from({ length: 6 }, () => `<div class="trow"><span></span><span class="skel" style="height:14px;width:60%;border-radius:4px;grid-column:2/-1"></span></div>`).join('');
  if (L.error) return emptyState('cloud_off', 'Couldn’t load the library', L.error);
  if (!L.ready) return emptyState('hourglass_top', 'Indexing your library…', 'The first scan can take a few minutes on a large library. This page fills in when it finishes.');
  return emptyState('folder_open', L.scopeN || S.lib.path.length ? 'Nothing matches' : 'No songs yet', L.scopeN || S.lib.path.length ? 'Clear a filter or pick another folder.' : 'Downloaded songs show up here.');
}
const libNotice = () => (S.lib.pg.pending ? `<div class="row b-m" style="gap:12px;background:var(--md-sc-high);border-radius:16px;padding:10px 16px">${ic('hourglass_top', 'v')}<span class="grow">Reading tags… ${S.lib.pg.pending.toLocaleString()} songs still loading. They appear as they finish.</span></div>` : '');

// The library is drawn in three parts so a keystroke or tap only redraws what changed:
// the shell (title, search box, view switch) on navigation, the body (filters, table, toolbar)
// on filter/sort/selection changes, and the aside (song details) when a song is picked.
// The server filters, sorts and pages the table; the browser holds only the LIB_PER rows on screen.
const LIB_PER = 50;
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
let libCur = null;   // the rows the table is currently showing, for select-all
function libPager() {
  const g = S.lib.pg;
  if (g.pages <= 1) return '';
  const from = (g.page - 1) * LIB_PER + 1, to = Math.min(g.total, g.page * LIB_PER);
  const btn = (p, icon, label, off) => `<button class="ib" data-act="libpage" data-p="${p}" ${off ? 'disabled' : ''} aria-label="${label}" title="${label}">${ic(icon)}</button>`;
  return `<nav class="row" aria-label="Pages" style="justify-content:center;gap:4px">
    ${btn(1, 'first_page', 'First page', g.page <= 1)}${btn(g.page - 1, 'chevron_left', 'Previous page', g.page <= 1)}
    <span class="b-m v" style="padding:0 12px;text-align:center">${from.toLocaleString()}–${to.toLocaleString()} of ${g.total.toLocaleString()}<span class="hide-sm"> · page ${g.page} of ${g.pages}</span></span>
    ${btn(g.page + 1, 'chevron_right', 'Next page', g.page >= g.pages)}${btn(g.pages, 'last_page', 'Last page', g.page >= g.pages)}</nav>`;
}
function libBodyHTML() {
  const L = S.lib, cols = colsOn(), g = L.pg;
  const d = { p: L.path.join('/'), rows: g.rows, folders: g.folders };
  libCur = { rows: d.rows, cols };
  const allSel = d.rows.length > 0 && d.rows.every((t) => L.sel.has(t.id));
  return `<div class="row wrap" style="gap:8px"><div class="row wrap lib-extra" style="gap:8px" role="group" aria-label="Filters">${CHIPS.map(([k, l]) => `<button class="chip ${L.chips.has(k) ? 'on' : ''}" aria-pressed="${L.chips.has(k)}" data-act="chip" data-k="${k}">${L.chips.has(k) ? ic('check') : ''}${l}</button>`).join('')}</div>
        <span class="b-m v" style="margin-left:auto">${g.loaded ? plural(g.count, 'track') : ''}</span>
        <button class="ib" data-act="libreload" title="Rescan library" aria-label="Rescan library">${ic('refresh')}</button>
        <button class="ib hide-sm" data-act="cols" title="Choose columns" aria-label="Choose columns">${ic('settings')}</button></div>
      ${libNotice()}
      <div class="tscroll"><div role="table" aria-label="Tracks" class="tbl" id="lib-tbl" style="--minw:${colMinW(cols)}px">
        ${tableHead(cols, allSel)}
        ${d.folders.map((f) => folderRow(f, d.p, cols)).join('')}${d.rows.map((t) => trackRow(t, cols)).join('')}
        ${!d.folders.length && !d.rows.length ? libEmpty() : ''}
      </div></div>${libPager()}
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
  tintEl(el, S.lib.focus != null ? trackOf(S.lib.focus) || S.lib.focusT : null);
  fillWaves();
}

// ── Waveform ─────────────────────────────────────────────────────────────
// The server makes it the first time a song is opened (and keeps it); here each file version is asked for once.
const waves = new Map();
function waveBars(path, v) {
  const k = path + '|' + v;
  if (!waves.has(k)) waves.set(k, api(`/api/library/waveform?path=${encodeURIComponent(path)}&v=${v}`).then((d) => d.bars)
    .catch((e) => { waves.delete(k); throw e; }));
  return waves.get(k);
}
const waveHTML = (t) => `<div class="wave" data-wave="${esc(t.path)}" data-v="${t.mtime || 0}" role="img" aria-label="Waveform"><span class="skel"></span></div>`;
function waveSVG(bars) {
  const n = bars.length;
  return `<svg viewBox="0 0 ${n * 3} 100" preserveAspectRatio="none" aria-hidden="true">${bars.map((b, i) => {
    const h = Math.max(3, b);
    return `<rect x="${i * 3}" y="${(100 - h) / 2}" width="2" height="${h}" rx="1" style="animation-delay:${Math.round(i * 300 / n)}ms"/>`;
  }).join('')}</svg>`;
}
// Fill every waveform placeholder on screen (details panel or the phone dialog).
function fillWaves() {
  $$('[data-wave]:not([data-done])').forEach((el) => {
    el.dataset.done = '1';
    waveBars(el.dataset.wave, el.dataset.v).then((bars) => { el.innerHTML = waveSVG(bars); },
      (e) => { el.classList.add('err'); el.innerHTML = `${ic('graphic_eq', '', 'font-size:18px')}<span>Couldn’t draw the waveform${e.message ? ': ' + esc(e.message) : ''}</span>`; });
  });
}

function libAsideHTML() {
  const L = S.lib;
  const focus = L.focus != null ? trackOf(L.focus) || L.focusT : null;
  return focus ? detailHTML(focus) : emptyState('music_note', 'Select a track', 'Pick a song to see its tags, file path and duplicates.');
}
const libTotal = () => (S.lib.pg.loaded ? `· ${plural(S.lib.pg.scopeN, 'track')} · ${fmtMB(S.lib.pg.scopeSize)}` : '');
function viewLibrary() {
  const L = S.lib;
  const crumbs = ['Library', ...L.path];
  return `<div class="lib-layout ${L.filtersOpen ? 'fopen' : ''}">
    <section class="pane grow" aria-labelledby="h-lib" style="padding:16px 16px 20px;display:flex;flex-direction:column;gap:14px;min-width:0">
      <div class="row wrap" style="gap:12px">
        ${L.path.length ? `<button class="ib" data-act="up" aria-label="Up one folder">${ic('arrow_back')}</button>` : ''}
        <div class="grow col"><h1 id="h-lib" class="hl-s ell">${esc(L.path.length ? L.path[L.path.length - 1] : 'Library')}</h1>
          <nav class="row wrap b-m v crumbs" aria-label="Breadcrumb">${crumbs.map((c, i) => i === crumbs.length - 1
            ? `<span>${esc(c)}</span>` : `<button class="btn text" style="height:28px;padding:0 6px" data-act="crumb" data-i="${i}">${esc(c)}</button><span>/</span>`).join('')}
            <span id="lib-total">${libTotal()}</span></nav></div>
        <label class="searchbar sm" style="width:300px;max-width:100%">${ic('search', 'v')}<input id="lq" type="search" enterkeyhint="search" value="${esc(L.q)}" placeholder="Search library" aria-label="Search library" data-input="lq" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">
          <button class="ib" data-act="clearlq" aria-label="Clear search" ${L.q ? '' : 'hidden'}>${ic('close')}</button>
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
}

// ═════════════════════════════════════════════════════════════════════════
// Library health
// ═════════════════════════════════════════════════════════════════════════
const KIND = { id: ['tag', 'Same ID'], tags: ['sell', 'Same tags'] };
const qLabel = (f) => (f.lossless ? 'Lossless' : `${Math.round((f.bitrate || 0) / 1000)} kbps`);
const MB = 1048576;

// A group's id comes from its files, so a choice made in the list survives the list being refreshed.
const dupKey = (paths) => { let h = 0; for (const ch of [...paths].sort().join('|')) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return h.toString(36); };
function mapDupGroups(res) {
  return (res.groups || []).map((g, gi) => {
    const f0 = g.files[0];
    return {
      id: 'd' + dupKey(g.files.map((f) => f.rel)), kind: g.match === 'title' ? 'tags' : 'id', title: `${f0.artist} — ${f0.title}`,
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
// Duplicates are live: the server groups the tag cache on every request, so there is nothing to scan.
let dupsSig = '';
async function loadDups(fresh) {
  try {
    const d = await api(`/api/library/duplicates${fresh ? '?fresh=1' : ''}`);
    if (!d.ready) return;
    const sig = JSON.stringify(d.groups);
    if (sig === dupsSig && S.h.dups) return;
    dupsSig = sig;
    S.h.dups = mapDupGroups(d);
    const ids = new Set(S.h.dups.map((g) => g.id));
    for (const k of Object.keys(S.h.keep)) if (!ids.has(k)) delete S.h.keep[k];
    if (S.route === 'health') render();
  } catch { /* the list stays as it was */ }
}
// Mistagged songs need the audio fingerprinted, so they are a scan; the server patches a finished result when files change.
async function loadScan(kind) {
  try {
    const d = await api(`/api/library/scan/${kind}`);
    const info = d.idle ? { idle: true } : { running: d.running, summary: d.summary, error: d.error, phase: d.phase };
    S.h.misInfo = { ...info, fingerprint: d.result ? d.result.fingerprint : true, fp_reason: d.result ? d.result.fp_reason : '', fp_failed: d.result ? d.result.fp_failed : 0 };
    S.h.mis = d.result ? mapMisGroups(d.result) : null; S.h.sel.mis.clear();
    render();
  } catch { /* leave what we have */ }
}
function refreshScans() { loadDups(true); if (S.h.mis !== null) loadScan('mistag'); }
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
  const info = S.h.misInfo;
  if (scanRunning() || (info && info.running)) return emptyState('hourglass_top', 'Scanning your library…', 'Fingerprinting takes a while on a big library. You can leave this page; the result is kept.');
  if (info && info.error) return emptyState('error', 'The scan failed', info.error);
  const stale = info && info.summary && /changed/i.test(info.summary);
  return `${emptyState(stale ? 'refresh' : 'radar', stale ? 'The library changed since the last scan' : `Not scanned yet`, hint)}
    <div class="row" style="justify-content:center"><button class="btn filled" data-act="scan">${ic('radar')}Scan audio</button></div>`;
}

function healthDups() {
  if (S.h.dups === null) return libGate(() => emptyState('hourglass_top', 'Looking for duplicates…'));
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
    return `<span class="b-m v">These file names don’t match what your naming format makes of their tags. Renaming fixes the file, not the tags. To move files into new folders, use the Folders tab.</span>
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

function healthBroken() {
  return libGate(() => {
    const list = brokenList(); const sel = S.h.sel.bad;
    return `<span class="b-m v">Files that can’t play: empty, cut short, or with no audio in them (they show as 0:00). Usually a download that was interrupted. Delete them, then download the song again.</span>
    ${list.length ? listRows('bad', list, (t) => `<span class="col grow" style="min-width:0"><span class="b-l ell">${esc(t.title)}</span><span class="b-m v ell">${esc(t.dir)}/${esc(t.file)}</span></span>
      <span class="b-s v hide-sm mono" style="white-space:nowrap">${t.bytes < 1024 * 1024 ? Math.max(0.1, t.bytes / 1024).toFixed(0) + ' KB' : t.size.toFixed(1) + ' MB'}</span><span class="tag error">${brokenWhy(t)}</span>`)
      : emptyState('task_alt', 'No broken songs', 'Every file in your library has audio in it.')}
    ${list.length ? `<div class="dock"><span class="b-l grow">${sel.size ? plural(sel.size, 'file') + ' selected' : plural(list.length, 'broken file')}</span>
      <button class="btn outlined danger" data-act="hdeletebad" ${sel.size ? '' : 'disabled'}>${ic('delete')}Delete${sel.size ? ' ' + sel.size : ''}</button></div>` : ''}`;
  });
}

// Folders: re-file the whole library by a path format (preview, then apply)
const orgMoves = () => S.h.org.ops.filter((o) => o.changed);
function healthOrganize() {
  const o = S.h.org; const run = o.phase === 'scanning' || o.phase === 'applying';
  const moves = orgMoves(); const errs = o.ops.filter((x) => x.error);
  const cap = 300;
  return `<span class="b-m v">Moves files into folders built from their tags. <b>{artist}</b> is the main artist only, so featured artists stay out of folder names. Preview first, then apply.</span>
    <div class="field"><div class="box"><label for="org-fmt">Path format</label><input id="org-fmt" value="${esc(o.fmt)}" data-input="orgfmt" autocomplete="off" ${run ? 'disabled' : ''}></div>
      <span class="help">Tokens: {artist} {album} {track} {title}</span></div>
    <div class="row wrap" style="gap:8px"><button class="btn tonal" data-act="orgpreview" ${run ? 'disabled' : ''}>${ic('preview')}Preview</button>
      ${o.phase === 'preview' && moves.length ? `<button class="btn filled" data-act="orgapply">${ic('drive_file_move')}Apply ${plural(moves.length, 'move')}</button>` : ''}</div>
    ${run ? `<div class="lp ${o.total ? '' : 'ind'}" role="progressbar" aria-label="Progress"><span class="a" style="width:${o.total ? Math.round(o.done / o.total * 100) : 30}%"></span><span class="t"></span></div>
      <span class="b-m v">${o.phase === 'scanning' ? 'Scanning' : 'Moving'} · ${o.done} / ${o.total} files${o.phase === 'applying' ? ` · ${o.moved} moved${o.errors ? ` · ${plural(o.errors, 'error')}` : ''}` : ''}</span>` : ''}
    ${o.msg ? `<span class="b-m v">${esc(o.msg)}</span>` : ''}
    ${o.phase === 'preview' ? `<div class="row wrap" style="gap:8px"><span class="tag primary">${plural(moves.length, 'file')} to move</span><span class="tag neutral">${o.ops.length - moves.length - errs.length} already correct</span>${errs.length ? `<span class="tag error">${plural(errs.length, 'error')}</span>` : ''}</div>
      ${moves.length ? `<div class="col" style="gap:8px">${moves.slice(0, cap).map((m) => `<div class="col" style="min-width:0"><span class="mono v ell" style="font-size:12px">${esc(m.src)}</span><span class="b-l ell">${esc(m.dst)}</span></div>`).join('')}${moves.length > cap ? `<span class="b-m v">… and ${moves.length - cap} more</span>` : ''}</div>`
        : !errs.length ? emptyState('task_alt', 'Everything is already where it belongs') : ''}
      ${errs.length ? `<div class="col" style="gap:4px"><span class="b-l">Errors</span>${errs.slice(0, 25).map((e) => `<span class="b-m v ell">${esc(e.src)}: ${esc(e.error)}</span>`).join('')}${errs.length > 25 ? `<span class="b-m v">… and ${errs.length - 25} more</span>` : ''}</div>` : ''}` : ''}`;
}
async function orgStream(kind, onEvent) {
  const res = await fetch(`/api/library/organize/${kind}`, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ format: S.h.org.fmt.trim() }) });
  if (!res.ok) { let m = ''; try { m = (await res.json()).error; } catch { /* not JSON */ } throw new Error(m || `${res.status} ${res.statusText}`); }
  const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = '';
  for (;;) {
    const { done, value } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split('\n\n'); buf = parts.pop();
    for (const part of parts) for (const line of part.split('\n')) if (line.startsWith('data: ')) { try { onEvent(JSON.parse(line.slice(6))); } catch { /* partial event */ } }
  }
}
let orgTick = 0;
const orgRender = () => { const t = Date.now(); if (t - orgTick > 250 && S.route === 'health') { orgTick = t; render(); } };

// ═════════════════════════════════════════════════════════════════════════
// Discover
// ═════════════════════════════════════════════════════════════════════════
let discTimer;
async function loadDiscover(refresh = false) {
  const D = S.disc; clearTimeout(discTimer);
  try {
    const d = await api(`/api/discover?recent=${D.recent ? 1 : 0}${refresh ? '&refresh=1' : ''}`);
    D.data = d; D.error = d.error || '';
    if (d.building && S.route === 'discover') discTimer = setTimeout(() => loadDiscover(), 2500);
  } catch (e) { D.error = e.message; }
  if (S.route === 'discover') render();
}
const discShown = () => {
  const d = S.disc.data, b = S.disc.basis;
  if (!d) return { releases: [], gaps: [], songs: [] };
  return { releases: b.has('artists') ? d.releases : [], gaps: b.has('artists') ? d.gaps : [], songs: d.songs.filter((x) => b.has(x.source)) };
};
function relCard(r, i) {
  const have = r.have ? `<span class="tag tertiary">${r.have} of ${r.track_count} in library</span>` : `<span class="tag neutral">${r.year || ''}${r.year ? ' · ' : ''}${r.type === 'album' ? 'Album' : 'Single'}</span>`;
  return `<div class="rel"><div class="cv">${coverTile({ title: r.title, kind: 'album', url: r.cover_url }, 184)}
      <button class="ib" data-act="dhide" data-k="releases" data-i="${i}" aria-label="Not interested in ${esc(r.title)}" title="Not interested">${ic('close')}</button></div>
    <div class="col" style="gap:2px;min-width:0"><span class="t-m ell">${esc(r.title)}</span><span class="b-m v ell">${esc(r.artist)}</span></div>
    <div class="row" style="justify-content:space-between;gap:8px">${have}<button class="ib tonal" data-act="ddl" data-k="releases" data-i="${i}" aria-label="Download ${esc(r.title)}">${ic('download')}</button></div></div>`;
}
function gapRow(g, i) {
  const miss = g.track_count - g.have;
  return `<div class="card" style="flex-direction:row;align-items:center;gap:14px">${coverTile({ title: g.title, kind: 'album', url: g.cover_url }, 56)}
    <div class="grow col" style="gap:8px;min-width:0"><div class="col"><span class="t-m ell">${esc(g.title)}</span><span class="b-m v ell">${esc(g.artist)} · ${g.have} of ${g.track_count} songs</span></div>
      <div class="lp" role="progressbar" aria-label="${g.have} of ${g.track_count} songs in library"><span class="a" style="width:${Math.round(g.have / g.track_count * 100)}%"></span><span class="t"></span></div></div>
    <button class="btn tonal" data-act="ddl" data-k="gaps" data-i="${i}">${ic('download')}<span>Get ${miss} <span class="hide-sm">missing</span></span></button>
    <button class="ib" data-act="dhide" data-k="gaps" data-i="${i}" aria-label="Not interested in ${esc(g.title)}" title="Not interested">${ic('visibility_off')}</button></div>`;
}
function songRow(t, i) {
  return `<div class="li" style="min-height:64px;padding:6px 8px 6px 12px">${coverTile({ title: t.title, kind: 'track', url: t.cover_url })}
    <div class="grow col" style="min-width:0"><span class="b-l ell">${esc(t.title)}</span><span class="b-m v ell">${esc(t.artists)}${t.album ? ' · ' + esc(t.album) : ''}</span></div>
    <span class="tag neutral hide-sm">${ic('auto_awesome')}${esc(t.reason)}</span>
    <span class="mono l-m v hide-sm" style="width:40px;text-align:right">${t.duration_ms ? fmtLen(Math.round(t.duration_ms / 1000)) : ''}</span>
    <button class="ib filled" data-act="ddl" data-k="songs" data-i="${i}" aria-label="Download ${esc(t.title)}">${ic('download')}</button>
    <button class="ib" data-act="dhide" data-k="songs" data-i="${i}" aria-label="Not interested in ${esc(t.title)}" title="Not interested">${ic('visibility_off')}</button></div>`;
}
const discSec = (title, sub, action = '') => `<div class="row" style="align-items:flex-end;gap:12px"><div class="grow col" style="gap:2px"><h2 class="t-l">${title}</h2><span class="b-m v">${sub}</span></div>${action}</div>`;
function discSide(d) {
  const hid = d.hidden || [];
  return `<aside class="disc-side">
    ${d.taste && d.taste.length ? `<div class="box"><h2 class="t-m">Your taste</h2><span class="b-m v" style="margin-top:-6px">Genres by number of songs</span>
      ${d.taste.map((g) => `<div class="col" style="gap:6px"><span class="b-m">${esc(g.name)}</span><div class="taste-bar"><span style="width:${g.weight}%"></span></div></div>`).join('')}</div>` : ''}
    <div class="box"><h2 class="t-m">Hidden suggestions</h2><span class="b-m v">Anything you mark “not interested” stays out of Discover. You can bring it back.</span>
      <button class="btn tonal" style="align-self:flex-start" data-act="dhidden" aria-expanded="${S.disc.hiddenOpen}" ${hid.length ? '' : 'disabled'}>${ic(S.disc.hiddenOpen ? 'visibility_off' : 'visibility')}${hid.length ? `Manage hidden (${hid.length})` : 'Nothing hidden'}</button>
      ${S.disc.hiddenOpen ? hid.map((h) => `<div class="row" style="gap:8px"><span class="grow col" style="min-width:0"><span class="b-m ell">${esc(h.title || h.url)}</span><span class="b-s v ell">${esc(h.sub || '')}</span></span><button class="btn text" data-act="dunhide" data-url="${esc(h.url)}">Restore</button></div>`).join('') : ''}</div>
    <div class="box"><h2 class="t-m">How this works</h2><span class="b-m v">Songs already in your library are never suggested. Downloads use your current quality and sources and are added to the queue at once.</span></div>
  </aside>`;
}
function viewDiscover() {
  const D = S.disc, d = D.data;
  const chip = (k, l) => `<button class="chip ${D.basis.has(k) ? 'on' : ''}" aria-pressed="${D.basis.has(k)}" data-act="dbasis" data-k="${k}">${D.basis.has(k) ? ic('check') : ''}${l}</button>`;
  const head = `<header class="row wrap" style="align-items:flex-end;gap:16px"><div class="grow col" style="gap:4px"><h1 id="h-disc" class="hl-m">Discover</h1>
      <span class="b-m v">Releases and songs you don’t have yet, picked from what’s in your library.</span></div>
      <button class="btn tonal" data-act="drefresh" ${d && d.building ? 'disabled' : ''}>${d && d.building ? '<span class="ms spin" aria-hidden="true">progress_activity</span>Working…' : `${ic('refresh')}Refresh`}</button></header>
    <div class="row wrap" style="gap:8px" role="group" aria-label="Based on"><span class="b-m v" style="margin-right:4px">Based on</span>${chip('artists', 'Artists you collect')}${chip('genres', 'Genres')}${chip('similar', 'Similar artists')}
      <button class="chip ${D.recent ? 'on' : ''}" aria-pressed="${D.recent}" data-act="drecent">${D.recent ? ic('check') : ''}Added this month</button></div>`;
  let body;
  const skel = Array.from({ length: 3 }, () => `<div class="li"><div class="lead skel"></div><div class="grow col" style="gap:8px"><span class="skel" style="height:14px;width:55%;border-radius:4px"></span><span class="skel" style="height:12px;width:35%;border-radius:4px"></span></div></div>`).join('');
  if (D.error && !(d && d.releases && (d.releases.length || d.songs.length))) body = emptyState('cloud_off', 'Couldn’t build suggestions', D.error);
  else if (!d || d.building && !d.releases.length && !d.songs.length) {
    body = `<div class="row" style="gap:10px"><span class="ms spin" aria-hidden="true">hourglass_top</span><span class="b-m v">${d && d.why === 'library' ? 'Waiting for the library scan to finish…' : 'Finding suggestions from your top artists…'}</span></div>${skel}`;
  } else if (d.state === 'empty') {
    body = emptyState('library_music', D.recent ? 'Nothing added this month' : 'Nothing to base suggestions on yet', D.recent ? 'Turn off “Added this month” to use your whole library.' : 'Discover learns from your library. Download a few songs, or wait for the library scan to finish.');
  } else {
    const sh = discShown();
    const any = sh.releases.length || sh.gaps.length || sh.songs.length;
    body = `<div class="disc"><div class="disc-main">
      ${sh.releases.length ? `<div class="col" style="gap:14px">${discSec('New from artists you collect', 'Releases by artists in your library that you don’t own yet')}<div class="rel-row">${sh.releases.map(relCard).join('')}</div></div>` : ''}
      ${sh.songs.length ? `<div class="col" style="gap:8px">${discSec('Songs you might like', d.similar === false ? 'Artists in your top genres' : 'Similar artists and genres from your library', `<button class="btn tonal" data-act="ddlall">${ic('download')}Download all ${sh.songs.length}</button>`)}<div class="col" style="gap:2px;margin-top:6px">${sh.songs.map(songRow).join('')}</div></div>` : ''}
      ${sh.gaps.length ? `<div class="col" style="gap:14px">${discSec('Complete your albums', 'You own most of these. Download what’s missing.')}<div class="col" style="gap:10px">${sh.gaps.map(gapRow).join('')}</div></div>` : ''}
      ${any ? '' : emptyState('explore_off', 'No suggestions for this selection', 'Try turning on another “Based on” option, or refresh.')}
    </div>${discSide(d)}</div>`;
  }
  return `<section class="pane" style="padding:28px;display:flex;flex-direction:column;gap:24px" aria-labelledby="h-disc">${head}${body}</section>`;
}
async function discQueue(urls, title, done) {
  try {
    await api('/api/download', { method: 'POST', body: { urls: urls.join('\n'), quality: S.quality, pre_title: urls.length === 1 ? title : '' } });
    done(); render(); snack(urls.length === 1 ? `Added “${title}” to the queue` : `Added ${plural(urls.length, 'item')} to the queue`); refreshJobs();
  } catch (e) { oops('Couldn’t queue it')(e); }
}
const discItem = (el) => { const k = el.dataset.k, i = +el.dataset.i; const it = discShown()[k][i]; return it && { k, it }; };
const discDrop = (k, urls) => { const d = S.disc.data; d[k] = d[k].filter((x) => !urls.includes(x.url)); };

function viewHealth() {
  const tabs = [['dups', 'content_copy', 'Duplicates', S.h.dups ? S.h.dups.length : '–'], ['mis', 'graphic_eq', 'Mistagged', S.h.mis ? S.h.mis.length : '–'],
    ['name', 'text_fields', 'Misnamed', S.lib.loaded && S.lib.ready ? misnamed().length : '–'], ['miss', 'label_off', 'Missing tags', S.lib.loaded && S.lib.ready ? missingList().length : '–'],
    ['bad', 'broken_image', 'Broken', S.lib.loaded && S.lib.ready ? brokenList().length : '–'], ['org', 'folder_managed', 'Folders', '']];
  const body = { dups: healthDups, mis: healthMis, name: healthNames, miss: healthMissing, bad: healthBroken, org: healthOrganize }[S.h.tab]();
  const b = scanInfo();
  const busy = !!b;
  return `<section class="pane" style="padding:24px 28px 20px;display:flex;flex-direction:column;gap:16px" aria-labelledby="h-health">
    <header class="row wrap" style="align-items:flex-end;gap:16px"><div class="grow col" style="gap:4px"><h1 id="h-health" class="hl-m">Library health</h1>
      <span class="b-m v">Duplicates, misnamed files, missing tags and broken files are read live from your library. Mistagged songs need a scan, because the audio has to be fingerprinted.</span></div>
      <button class="btn tonal" data-act="scan" ${busy ? 'disabled' : ''}>${busy ? `<span class="ms spin" aria-hidden="true">progress_activity</span><span data-scan-health>${esc(b ? b.text : 'Scanning…')}</span>` : `${ic('radar')}Scan audio`}</button></header>
    ${busy ? `<div class="lp ${b && b.pct != null ? '' : 'ind'}" role="progressbar" aria-label="Scan progress" data-scan-bar><span class="a" style="width:${b && b.pct != null ? b.pct : 30}%"></span><span class="t"></span></div>` : ''}
    <div class="tabs" role="tablist" aria-label="Issue type">${tabs.map(([k, i, l, n]) => `<button class="tab" role="tab" aria-selected="${S.h.tab === k}" data-act="htab" data-k="${k}">${ic(i, S.h.tab === k ? 'f' : '')}<span class="t-s">${l}</span>${n === '' ? '' : `<span class="tag ${S.h.tab === k ? 'primary' : 'neutral'}" style="height:20px;padding:0 8px">${n}</span>`}</button>`).join('')}</div>
    ${body}</section>`;
}

// ═════════════════════════════════════════════════════════════════════════
// Settings
// ═════════════════════════════════════════════════════════════════════════
const SECTIONS = [['appearance', 'palette', 'Appearance'], ['naming', 'text_fields', 'File naming'], ['downloads', 'download', 'Downloads'], ['sources', 'hub', 'Sources'],
  ['extensions', 'extension', 'Extensions'], ['metadata', 'sell', 'Metadata'], ['lb', 'queue_music', 'ListenBrainz'], ['network', 'vpn_lock', 'Network & VPN'], ['logging', 'receipt_long', 'Logging'], ['info', 'info', 'Info']];
// What the container log can show; keys match applog.py. Errors and warnings always print.
const LOG_CATS = [['vpn', 'VPN', 'Connecting, connected, reconnecting, and the exit IP'], ['downloads', 'Downloads', 'Each song as it starts, finishes or fails'],
  ['enrich', 'Enrichment', 'Batch enrichment, repaired albums and per-song errors'], ['system', 'Startup', 'Start-up summary and settings changes'],
  ['library', 'Library', 'Index scans and health scans'], ['requests', 'Web requests', 'Every request the web interface makes (very chatty)'],
  ['detail', 'Diagnostic detail', 'Everything else: pre-scans, provider chatter, internals']];
const LOG_PRESETS = [['Minimal', ['vpn', 'downloads', 'enrich', 'system']], ['Standard', ['vpn', 'downloads', 'enrich', 'system', 'library']], ['Everything', LOG_CATS.map((c) => c[0])]];
const logPreset = () => { const on = [...S.set.log].sort().join(); const p = LOG_PRESETS.find(([, k]) => [...k].sort().join() === on); return p ? p[0] : ''; };
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
      ${[['auto', 'Auto'], ['light', 'Light'], ['dark', 'Dark']].map(([k, l]) => `<button role="radio" aria-checked="${store.get('mode', 'auto') === k}" data-act="mode" data-k="${k}">${store.get('mode', 'auto') === k ? ic('check') : ''}${l}</button>`).join('')}</div>
      <span class="b-s v">Colour and mode are saved in this browser.</span></div></section>`,
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
  logging: () => `<section class="section" aria-labelledby="h-set"><h2 id="h-set" class="t-l">Logging</h2>
    <p class="b-m v">What the container prints to <span class="mono">docker logs</span>. Errors and warnings are always shown. Changes apply immediately.</p>
    <div class="seg" role="radiogroup" aria-label="Preset" style="align-self:flex-start">${LOG_PRESETS.map(([l]) => `<button role="radio" aria-checked="${logPreset() === l}" data-act="logpreset" data-k="${l}">${logPreset() === l ? ic('check') : ''}${l}</button>`).join('')}</div>
    <div class="col">${LOG_CATS.map(([k, l, h]) => `<div class="row" style="min-height:56px;gap:16px"><span class="grow col"><span class="b-l">${l}</span><span class="b-m v">${h}</span></span>
      <button class="sw" role="switch" aria-checked="${S.set.log.includes(k)}" aria-label="Log ${l}" data-act="logcat" data-k="${k}"><span class="th">${S.set.log.includes(k) ? ic('check') : ''}</span></button></div>`).join('')}</div>
    <pre class="mono" style="background:var(--md-sc-low);border-radius:16px;padding:14px 16px;margin:0;overflow:auto;font-size:12px;line-height:18px;color:var(--md-on-surface-variant)">21:35:09  INFO   VPN       Connected · tun0
21:36:10  INFO   DOWNLOAD  Downloading  Bob Sinclar – World, Hold On
21:36:48  ERROR  ENRICH    Failed       World, Hold On: Unrecognised audio format</pre></section>`,
  info: () => {
    const v = S.ver;
    const installed = v ? (v.installed === 'unknown' ? 'Unknown' : v.installed) : (S.verError ? '—' : 'Checking…');
    return `<section class="section" aria-labelledby="h-set"><h2 id="h-set" class="t-l">Info</h2>
    <div class="li" style="background:var(--md-sc-low);border-radius:20px">${ic('info', 'v')}<span class="grow col"><span class="b-l">SpotiFLAC version</span><span class="b-m v mono">${esc(installed)}</span></span></div>
    ${v && v.update_available ? `<div class="li" style="background:var(--md-primary-container);color:var(--md-on-primary-container);border-radius:20px">${ic('system_update', 'f')}<span class="grow col"><span class="b-l">Update available</span><span class="b-m">SpotiFLAC <span class="mono">${esc(v.latest)}</span> is out. You have <span class="mono">${esc(v.installed)}</span>.</span></span></div>`
      : v ? `<span class="b-m v">${ic('check_circle', 'v', 'font-size:18px;vertical-align:-4px')} You’re on the latest version.</span>` : ''}</section>`;
  },
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
const VIEWS = { download: viewDownload, library: viewLibrary, discover: viewDiscover, health: viewHealth, settings: viewSettings };
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
  if (S.route === 'library') tintDetail();
  $$('#view [data-ind]').forEach((cb) => { cb.indeterminate = true; });
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
const trackOf = (path) => S.lib.pg.rows.find((t) => t.path === path) || S.lib.tracks.find((t) => t.path === path);
// The server re-indexes in the background after file changes, so look again a moment later.
const later = (ms = 2500) => setTimeout(() => loadTracks(true), ms);
const firstError = (d) => (d && d.errors && d.errors[0]) || '';
function downloadFile(href) { const a = document.createElement('a'); a.href = href; a.rel = 'noopener'; document.body.appendChild(a); a.click(); a.remove(); }

// Progress of single-song enrichment (per path), shown under the Enrich button of that song's details.
const ENR = {};
function enrichHTML(path) {
  const e = ENR[path];
  if (!e) return '';
  const line = (st) => `<div class="row" style="gap:8px;align-items:flex-start"><span class="ms ${st.pending ? 'spin' : ''}" aria-hidden="true" style="font-size:18px;color:${st.pending ? 'var(--md-primary)' : 'var(--md-outline)'}">${st.pending ? 'progress_activity' : 'check'}</span><span class="b-m ${st.pending ? '' : 'v'} grow" style="word-break:break-word">${esc(st.text)}</span></div>`;
  const res = e.result ? `<span class="tag ${e.result.error ? 'error' : 'primary'}" style="align-self:flex-start;height:auto;min-height:24px;white-space:normal;padding:4px 10px">${ic(e.result.error ? 'error' : 'check_circle')}${esc(e.result.text)}</span>` : '';
  return `<div class="pathbox col" style="gap:6px;font-family:inherit">${e.steps.map(line).join('')}</div>${res}`;
}
function paintEnrich(path) {
  for (const el of document.querySelectorAll('[data-enr]')) if (el.dataset.enr === path) el.innerHTML = enrichHTML(path);
}
function enrichOne(path) {
  if (ENR[path] && !ENR[path].result) return;                       // already running
  const e = ENR[path] = { steps: [], result: null, es: null };
  const fail = (text) => { e.steps.forEach((st) => { st.pending = false; }); e.result = { error: true, text }; e.es && e.es.close(); paintEnrich(path); };
  const es = e.es = new EventSource('/api/library/enrich-one?path=' + encodeURIComponent(path));
  es.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === 'step') {
      const st = e.steps.find((x) => x.id === m.id);
      if (st) { st.text = m.text; st.pending = !!m.pending; } else e.steps.push({ id: m.id, text: m.text, pending: !!m.pending });
    } else if (m.type === 'result') {
      es.close();
      e.steps.forEach((st) => { st.pending = false; });
      e.result = m.error ? { error: true, text: m.error }
        : { text: [m.enriched ? 'Enriched' : 'Already complete — nothing changed', m.moved ? 'file moved' : '', `${m.elapsed}s`].filter(Boolean).join(' · ') };
      if (!m.error) later(800);                                      // new tags: look at the row again
    } else if (m.type === 'error') return fail(m.msg || 'Enrichment failed');
    paintEnrich(path);
  };
  es.onerror = () => { if (!e.result) fail('Connection lost'); };
  paintEnrich(path);
}

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
A.pasteq = () => { navigator.clipboard?.readText().then((t) => { const tr = pastedTracks(t); if (tr) return quickQueue(tr); if (t) { S.q = t.trim(); scheduleSearch(); render(); $('#q')?.focus(); } }).catch(() => snack('Clipboard is blocked by the browser — paste with Ctrl+V')); };
A.clearlq = () => { const i = $('#lq'); if (i) { i.value = ''; i.focus(); INPUT.lq(i); } };
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
// Artist card (see artistCard)
A.artopen = (el) => {
  const a = el.dataset.a, o = S.art.open;
  if (o.has(a)) o.delete(a); else { o.add(a); if (!S.art.rel[a] || S.art.rel[a].error) return loadReleases(a); }
  renderResults();
};
A.artload = (el) => loadReleases(el.dataset.a);
A.relopen = (el) => {
  const r = el.dataset.r, o = S.art.albOpen;
  if (o.has(r)) o.delete(r); else { o.add(r); if (!S.art.trk[r] || S.art.trk[r].error) return loadRelTracks(r); }
  renderResults();
};
A.relload = (el) => loadRelTracks(el.dataset.r);
A.artsel = async (el) => {
  const a = el.dataset.a, sel = artSel(a);
  if (artState(a) !== 'none') { sel.rel.clear(); sel.trk.clear(); return renderResults(); }
  // Selecting an artist that hasn't been opened yet: fetch the discography first, and show it.
  if (!relsOf(a).length) { S.art.open.add(a); await loadReleases(a); }
  for (const r of relsOf(a)) sel.rel.add(r.url);
  sel.trk.clear();
  renderResults();
};
A.relsel = (el) => {
  const sel = artSel(el.dataset.a), r = el.dataset.r;
  if (relState(sel, r) === 'none') sel.rel.add(r); else sel.rel.delete(r);
  sel.trk.delete(r);
  renderResults();
};
A.trksel = (el) => {
  const sel = artSel(el.dataset.a), r = el.dataset.r, t = el.dataset.t;
  const all = S.art.trk[r].list.map((x) => x.url);
  // Unticking one song of a whole release turns it into "every other song".
  const picked = sel.rel.has(r) ? new Set(all) : new Set(sel.trk.get(r) || []);
  if (picked.has(t)) picked.delete(t); else picked.add(t);
  sel.rel.delete(r); sel.trk.delete(r);
  if (picked.size === all.length) sel.rel.add(r); else if (picked.size) sel.trk.set(r, picked);
  renderResults();
};
A.artclear = (el) => { const sel = artSel(el.dataset.a); sel.rel.clear(); sel.trk.clear(); renderResults(); };
A.artdl = async (el) => {
  const a = el.dataset.a, sel = artSel(a);
  const urls = [...sel.rel, ...[...sel.trk.values()].flatMap((s) => [...s])];
  if (!urls.length) return;
  const { songs } = artPicked(a);
  try {
    await api('/api/download', { method: 'POST', body: { urls: urls.join('\n'), quality: S.quality } });
    sel.rel.clear(); sel.trk.clear();
    renderResults();
    snack(`Queued ${plural(songs, 'song')}${urls.length > 1 ? ` in ${plural(urls.length, 'download')}` : ''} · songs you already have are skipped`);
    refreshJobs();
  } catch (e) { oops('Couldn’t queue it')(e); }
};
const jobAct = (path, method, fail) => async (el) => { try { await api(path(el.dataset.id), { method }); refreshJobs(); } catch (e) { oops(fail)(e); } };
A.jcancel = jobAct((id) => `/api/jobs/${encodeURIComponent(id)}/cancel`, 'POST', 'Couldn’t cancel');
A.jretry = jobAct((id) => `/api/jobs/${encodeURIComponent(id)}/retry`, 'POST', 'Couldn’t retry');
A.jremove = jobAct((id) => `/api/jobs/${encodeURIComponent(id)}`, 'DELETE', 'Couldn’t remove');
const redrawQueue = (focusId) => {
  const side = $('#dl-side'); if (S.route !== 'download' || !side) return;
  side.innerHTML = dlSideHTML();
  if (focusId) side.querySelector(`[data-act=qpick][data-id="${CSS.escape(focusId)}"]`)?.focus();
};
A.qselect = () => { S.qsel = S.qsel ? null : new Set(); redrawQueue(); };
A.qpick = (el) => { const id = el.dataset.id; if (S.qsel.has(id)) S.qsel.delete(id); else S.qsel.add(id); redrawQueue(id); };
A.qall = () => { const q = queueJobs(); if (S.qsel.size === q.length) S.qsel.clear(); else q.forEach((j) => S.qsel.add(j.id)); redrawQueue(); };
A.qbulk = async (el) => {
  const k = el.dataset.k, picked = queueJobs().filter((j) => S.qsel.has(j.id));
  const live = (j) => j.status === 'queued' || j.status === 'running', dead = (j) => j.status === 'error' || j.status === 'cancelled';
  const id = (j) => encodeURIComponent(j.id);
  const todo = k === 'retry' ? picked.filter(dead).map((j) => [`/api/jobs/${id(j)}/retry`, 'POST'])
    : k === 'cancel' ? picked.filter(live).map((j) => [`/api/jobs/${id(j)}/cancel`, 'POST'])
    : picked.map((j) => (live(j) ? [`/api/jobs/${id(j)}/cancel`, 'POST'] : [`/api/jobs/${id(j)}`, 'DELETE']));
  if (k === 'remove' && picked.some((j) => j.status === 'running') && !await confirmDlg('Remove downloads', `${plural(picked.filter((j) => j.status === 'running').length, 'download')} still running will be stopped.`, 'Remove')) return;
  const res = await Promise.all(todo.map(([path, method]) => api(path, { method }).then(() => true, () => false)));
  const ok = res.filter(Boolean).length, bad = res.length - ok;
  S.qsel.clear();
  snack(`${{ retry: 'Retrying', cancel: 'Cancelled', remove: 'Removed' }[k]} ${plural(ok, 'download')}${bad ? ` · ${bad} failed` : ''}`);
  await refreshJobs(); redrawQueue();
};
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

// Discover
A.ddl = (el) => { const x = discItem(el); if (x) discQueue([x.it.url], x.it.title, () => discDrop(x.k, [x.it.url])); };
A.ddlall = () => { const l = discShown().songs; if (l.length) discQueue(l.map((t) => t.url), '', () => discDrop('songs', l.map((t) => t.url))); };
A.dhide = async (el) => {
  const x = discItem(el); if (!x) return; const { k, it } = x;
  try { await api('/api/discover/hide', { method: 'POST', body: { url: it.url, title: it.title, sub: it.artist || it.artists || '' } }); } catch (e) { return oops('Couldn’t hide it')(e); }
  discDrop(k, [it.url]); S.disc.data.hidden = [{ url: it.url, title: it.title, sub: it.artist || it.artists || '' }, ...(S.disc.data.hidden || [])];
  render();
  snack(`Hidden “${it.title}”`, { label: 'Undo', fn: () => api('/api/discover/unhide', { method: 'POST', body: { url: it.url } }).then(() => loadDiscover()).catch(oops('Couldn’t undo')) });
};
A.dunhide = (el) => api('/api/discover/unhide', { method: 'POST', body: { url: el.dataset.url } }).then(() => loadDiscover()).catch(oops('Couldn’t restore'));
A.dhidden = () => { S.disc.hiddenOpen = !S.disc.hiddenOpen; render(); };
A.dbasis = (el) => { const b = S.disc.basis, k = el.dataset.k; if (b.has(k)) b.delete(k); else b.add(k); render(); };
A.drecent = () => { S.disc.recent = !S.disc.recent; S.disc.data = null; render(); loadDiscover(); };
A.drefresh = () => { if (S.disc.data) S.disc.data.building = true; render(); loadDiscover(true); };

// Library
A.cd = (el) => { S.lib.path = el.dataset.path.split('/'); S.lib.focus = null; render(); libQuery(); };
A.up = () => { S.lib.path.pop(); render(); libQuery(); };
A.crumb = (el) => { S.lib.path = S.lib.path.slice(0, +el.dataset.i); render(); libQuery(); };
A.view = (el) => { S.lib.view = el.dataset.k; render(); libQuery(); };
A.chip = (el) => { const c = S.lib.chips; c.has(el.dataset.k) ? c.delete(el.dataset.k) : c.add(el.dataset.k); renderLib(); libQuery(); };
A.libpage = (el) => { S.lib.page = +el.dataset.p; loadPage({ top: true }); };
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
  store.set('libsort', S.lib.sort); renderLib(); libQuery();
};
A.cols = () => dialog('Columns', `<p class="b-m v" style="margin-bottom:12px">Choose what the table shows. Click a column heading to sort by it.</p><div id="col-body">${colBody()}</div>`, [{ key: 'done', label: 'Done', cls: 'filled' }]);
A.coltoggle = (el) => {
  const k = el.dataset.k, c = S.lib.cols, i = c.indexOf(k);
  i < 0 ? c.push(k) : c.splice(i, 1);
  const unsort = i >= 0 && S.lib.sort && S.lib.sort.k === k;
  if (unsort) { S.lib.sort = null; store.set('libsort', null); }
  store.set('libcols', c); $('#col-body').innerHTML = colBody(); renderLib(); if (unsort) libQuery();
};
A.colreset = () => {
  S.lib.cols = [...DEFAULT_COLS]; store.set('libcols', S.lib.cols);
  const unsort = S.lib.sort && !colsOn().some((c) => c.id === S.lib.sort.k);
  if (unsort) { S.lib.sort = null; store.set('libsort', null); }
  $('#col-body').innerHTML = colBody(); renderLib(); if (unsort) libQuery();
};
A.selall = (el) => {
  const rows = libCur ? libCur.rows : [];
  rows.forEach((t) => (el.checked ? S.lib.sel.add(t.id) : S.lib.sel.delete(t.id)));
  $$('#lib-tbl [data-act=sel]').forEach((cb) => { cb.checked = S.lib.sel.has(cb.dataset.id); const r = cb.closest('.trow'); if (r) r.classList.toggle('sel', cb.checked); });
  libSelPatch();
};
A.clearsel = () => { S.lib.sel.clear(); renderLib(); };
A.focus = (el) => {
  const prev = S.lib.focus; S.lib.focus = el.dataset.id; S.lib.focusT = trackOf(S.lib.focus) || null;
  if (window.innerWidth < 1100) {
    const t = trackOf(S.lib.focus);
    if (t) { dialog('Song details', `<div class="col" style="gap:16px">${detailHTML(t, true)}</div>`, [{ key: 'close', label: 'Close' }]).then(() => clearTint($('#dlg'))); tintEl($('#dlg'), t); fillWaves(); }
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
A.enrich = (el) => { const ids = idsOf(el); if (ids.length === 1) enrichOne(ids[0]); else enrichPaths(ids); };
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
    try { await api(`/api/library/file?path=${encodeURIComponent(p)}`, { method: 'DELETE' }); gone++; S.lib.tracks = S.lib.tracks.filter((t) => t.path !== p); S.lib.pg.rows = S.lib.pg.rows.filter((t) => t.path !== p); S.lib.sel.delete(p); if (S.lib.focus === p) S.lib.focus = null; } catch (e) { err = e.message; }
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
    const d = await api('/api/library/scan/mistag', { method: 'POST' });
    snack(d.started === false ? 'A scan is already running' : 'Fingerprinting your library for mistagged songs…');
    refreshTasks();
  } catch (e) { oops('Couldn’t start the scan')(e); }
};
A.hsel = (el) => { const s = S.h.sel[el.dataset.set]; const id = el.dataset.id; el.checked ? s.add(id) : s.delete(id); render(); };
A.hselall = (el) => {
  const set = el.dataset.set; const list = { name: misnamed, miss: missingList, bad: brokenList }[set]();
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
A.hdeletebad = async () => {
  const files = [...S.h.sel.bad];
  if (!(await confirmDlg(`Delete ${plural(files.length, 'broken file')}?`, 'The files are removed from disk. This can’t be undone.', 'Delete'))) return;
  try {
    const d = await api('/api/library/mistag/delete', { method: 'POST', body: { files } });
    const gone = new Set(d.removed || []);
    S.lib.tracks = S.lib.tracks.filter((t) => !gone.has(t.path));
    gone.forEach((p) => S.h.sel.bad.delete(p));
    snack(firstError(d) ? `Deleted ${gone.size}. ${firstError(d)}` : `Deleted ${plural(gone.size, 'file')}`);
    render(); later(); loadAllTracks(true, true);
  } catch (e) { oops('Couldn’t delete')(e); }
};
A.renamenames = () => {
  const sel = S.h.sel.name; const paths = (sel.size ? [...sel] : misnamed().map((t) => t.id));
  sel.clear(); renamePaths(paths);
};
A.enrichmiss = () => { const sel = S.h.sel.miss; const paths = sel.size ? [...sel] : missingList().map((t) => t.id); sel.clear(); enrichPaths(paths); };

A.orgpreview = async () => {
  const o = S.h.org; if (!o.fmt.trim()) return;
  Object.assign(o, { phase: 'scanning', done: 0, total: 0, ops: [], msg: '' }); render();
  try {
    await orgStream('preview', (ev) => {
      if (ev.type === 'total' || ev.type === 'progress') { o.total = ev.total; o.done = ev.done || 0; orgRender(); }
      else if (ev.type === 'done') o.ops = ev.ops;
    });
    o.phase = 'preview';
  } catch (e) { o.phase = 'idle'; o.msg = `Preview failed: ${e.message}`; }
  render();
};
A.orgapply = async () => {
  const o = S.h.org; const n = orgMoves().length;
  if (!await confirmDlg('Move files', `This moves ${plural(n, 'file')} into new folders and removes empty folders. It can’t be undone from here.`, 'Move', false)) return;
  Object.assign(o, { phase: 'applying', done: 0, total: 0, moved: 0, errors: 0, msg: '' }); render();
  let res = null;
  try {
    await orgStream('apply', (ev) => {
      if (ev.type === 'total') o.total = ev.total;
      else if (ev.type === 'progress') { Object.assign(o, { total: ev.total, done: ev.done, moved: ev.moved, errors: ev.errors }); orgRender(); }
      else if (ev.type === 'done') res = ev;
    });
  } catch (e) { o.phase = 'idle'; o.msg = `Move failed: ${e.message}`; render(); return; }
  o.phase = 'idle'; o.ops = [];
  if (res) { o.msg = `Moved ${plural(res.moved, 'file')}${res.errors ? ` · ${plural(res.errors, 'error')}` : ''}`; snack(o.msg); }
  render(); refreshScans(); loadTracks(true);
};

// Settings
A.section = (el) => { S.section = el.dataset.k; render(); };
A.scheme = (el) => { store.set('scheme', el.dataset.k); applyTheme(); render(); };
A.mode = (el) => { store.set('mode', el.dataset.k); applyTheme(); render(); };
A.theme = () => { store.set('mode', document.documentElement.dataset.mode === 'dark' ? 'light' : 'dark'); applyTheme(); render(); };
A.logpreset = (el) => { S.set.log = [...LOG_PRESETS.find(([l]) => l === el.dataset.k)[1]]; saved(); render(); };
A.logcat = (el) => { const l = S.set.log, k = el.dataset.k; S.set.log = l.includes(k) ? l.filter((x) => x !== k) : [...l, k]; saved(); render(); };
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
    isrc: r.isrc, unreadable: !!r.unreadable, bytes: r.size, mtime: Math.round(r.mtime || 0), addedDays: Math.max(0, Math.floor((Date.now() / 1000 - r.mtime) / 86400)), expected: r.expected,
    csrc: r.cpath ? { path: r.cpath, mtime: Math.round(r.cmtime || 0) } : null,   // the album's cover file (paged rows)
  };
}
let tracksTimer, pageTimer, pageSeq = 0;
// The library page asks the server for one page of its table; any change of folder, view, filter,
// search or sort starts again at page 1.
function libQuery() { S.lib.page = 1; loadPage({ top: true }); }
// `fresh` makes the server re-check the files for changes (after we changed some); `poll` is a
// background re-ask while it reads or checks tags, which neither dims the table nor redraws it unchanged.
async function loadPage({ top = false, fresh = false, poll = false } = {}) {
  clearTimeout(pageTimer);
  const L = S.lib, seq = ++pageSeq;
  const qs = new URLSearchParams({ page: L.page, per: LIB_PER, path: L.path.join('/'), view: L.view, q: L.q.trim(), chips: [...L.chips].join(',') });
  if (L.sort) { qs.set('sort', L.sort.k); qs.set('dir', L.sort.dir); }
  if (fresh) qs.set('fresh', '1');
  const body = $('#lib-body'); if (body && L.pg.loaded && !poll) body.style.opacity = '.6';
  const again = () => { if (S.route === 'library') loadPage({ poll: true }); };
  try {
    const d = await api(`/api/library/tracks?${qs}`);
    if (seq !== pageSeq) return;
    if (d.lost) { L.path = []; L.page = 1; render(); return loadPage(); }
    const sig = JSON.stringify([d.tracks, d.folders, d.total, d.page, d.pages, d.pending, d.scope]);
    if (poll && sig === L.pgSig && L.pg.loaded && !L.pg.error) {
      if (!d.ready || d.pending || d.checking) pageTimer = setTimeout(again, 2500);
      return;
    }
    L.pgSig = sig;
    L.page = d.page;
    L.pg = { loaded: true, ready: d.ready, pending: d.pending || 0, error: '', rows: (d.tracks || []).map(adaptTrack), folders: d.folders || [],
      count: d.count || 0, total: d.total || 0, page: d.page || 1, pages: d.pages || 1, scopeN: d.scope ? d.scope.tracks : 0, scopeSize: d.scope ? d.scope.size / MB : 0 };
    const f = L.focus != null && L.pg.rows.find((t) => t.path === L.focus); if (f) L.focusT = f;
    if (S.route === 'library' && $('#lib-body')) {
      renderLib({ aside: !!f });
      const tot = $('#lib-total'); if (tot) tot.textContent = libTotal();
      const tbl = $('#lib-tbl'); if (top && tbl && tbl.getBoundingClientRect().top < 0) tbl.scrollIntoView({ block: 'start' });
    } else if (S.route === 'library') render();
    if (!d.ready || d.pending || d.checking) pageTimer = setTimeout(again, 2500);
  } catch (e) {
    if (seq !== pageSeq) return;
    L.pg.error = e.message; L.pg.loaded = true; if (S.route === 'library') render();
    pageTimer = setTimeout(again, 8000);
  } finally {
    const b = $('#lib-body'); if (b && seq === pageSeq) b.style.opacity = '';
  }
}
// Library data changed: refresh what's on screen. Library health needs every song; the library page needs one page.
function loadTracks(quiet) {
  if (S.route === 'library') loadPage({ fresh: true });
  if (S.route === 'health') loadAllTracks(quiet, true);
  else S.lib.loaded = false;   // fetched again on the next visit to health
}
async function loadAllTracks(quiet, fresh) {
  clearTimeout(tracksTimer);
  try {
    const d = await api(`/api/library/tracks${fresh ? '?fresh=1' : ''}`);
    const L = S.lib;
    L.ready = d.ready; L.pending = d.pending || 0; L.error = '';
    L.tracks = (d.tracks || []).map(adaptTrack); L.loaded = true;
    const have = new Set(L.tracks.map((t) => t.path));
    for (const k of Object.keys(S.h.sel)) for (const p of [...S.h.sel[k]]) if (!have.has(p) && k !== 'mis') S.h.sel[k].delete(p);
    rebuildDirCovers();
    if (S.route === 'health') { loadDups(); render(); } else renderChrome();
    if (!d.ready || d.pending || d.checking) tracksTimer = setTimeout(() => { if (S.route === 'health') loadAllTracks(true); else S.lib.loaded = false; }, 2500);
  } catch (e) {
    S.lib.error = e.message; S.lib.loaded = true; if (S.route === 'health') render();
    tracksTimer = setTimeout(() => { if (S.route === 'health') loadAllTracks(true); else S.lib.loaded = false; }, 8000);
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
let vpnBusy = false;
async function loadVpn() {
  if (vpnBusy) return;
  vpnBusy = true;
  try {
    const d = await api('/api/vpn', { timeout: 15000 });
    const next = { known: true, on: !!d.connected, since: d.connected_since || null };
    const changed = !S.vpn.known || S.vpn.on !== next.on;
    S.vpn = next;
    if (changed) renderChrome();
  } catch { /* keep the last known state */ } finally { vpnBusy = false; }
}
async function loadVersion() {
  try { S.ver = await api('/api/spotiflac/version'); S.verError = false; } catch { S.verError = true; }
  if (S.route === 'settings' && S.section === 'info') render();
}
function enterRoute() {
  if (S.route === 'discover') loadDiscover();
  if (S.route === 'library') loadPage();
  if (S.route === 'health' && !S.lib.loaded) loadAllTracks();
  if (S.route === 'health') {
    loadDups();
    if (S.h.mis === null && !S.h.misInfo) loadScan('mistag');
  }
  if (S.route === 'settings') { loadExt(); loadVersion(); }
}
// Retry countdowns tick every second without redrawing the queue.
function tickRetries() {
  const now = Date.now();
  $$('[data-retry-at]').forEach((el) => {
    const j = S.jobs.find((x) => x.id === el.closest('[data-retry-job]').dataset.retryJob);
    if (j) el.textContent = retryLine(j);
  });
  $$('[data-retry-bar]').forEach((el) => {
    const at = +el.dataset.retryBar, span = +el.dataset.retrySpan;
    el.style.width = Math.min(100, Math.max(0, 100 - ((at - now) / span) * 100)) + '%';
  });
}
function boot() {
  loadSettings(); refreshJobs(); refreshTasks(); loadVpn();
  setInterval(() => { if (!document.hidden) refreshJobs(); }, 2500);
  setInterval(() => { if (!document.hidden && S.route === 'download') tickRetries(); }, 1000);
  setInterval(() => { if (!document.hidden) refreshTasks(); }, 3000);
  setInterval(() => { if (!document.hidden) loadVpn(); }, 10000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { refreshJobs(); refreshTasks(); loadVpn(); } });
}

// ── Inputs (typing never re-renders settings fields, so focus and caret stay put) ──
let lqTimer;
const INPUT = {
  orgfmt: (el) => { S.h.org.fmt = el.value; if (S.h.org.phase === 'preview') { S.h.org.phase = 'idle'; render(); } },
  seed: (el) => { store.set('seed', el.value); store.set('scheme', 'custom'); applyTheme(); },
  q: (el) => { S.q = el.value; scheduleSearch(); renderResults(); },
  lq: (el) => { S.lib.q = el.value; const c = $('[data-act=clearlq]'); if (c) c.hidden = !el.value; clearTimeout(lqTimer); lqTimer = setTimeout(libQuery, 200); },
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
document.addEventListener('paste', (e) => {
  if (!e.target || e.target.id !== 'q') return;
  const tr = pastedTracks(e.clipboardData && e.clipboardData.getData('text'));
  if (tr) { e.preventDefault(); quickQueue(tr); }
});
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

// Shared from another app (the manifest's share target): drop a Spotify link into the search box.
(() => {
  const q = new URLSearchParams(location.search);
  if (!q.has('text') && !q.has('url') && !q.has('title')) return;
  const hit = [q.get('url'), q.get('text'), q.get('title')].map((x) => (x || '').match(/https?:\/\/open\.spotify\.com\/\S+/)).find(Boolean);
  history.replaceState(null, '', location.pathname + '#/download');
  const tr = hit && pastedTracks(hit[0]);
  if (tr) setTimeout(() => quickQueue(tr), 0); else if (hit) { S.q = hit[0]; scheduleSearch(); }
})();
// Offline copy of the app and update prompt. Browsers only allow this on https or localhost.
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').then((reg) => {
    const ask = (w) => snack('A new version is ready', { label: 'Reload', fn: () => w.postMessage('skip') });
    const watch = (w) => w.addEventListener('statechange', () => { if (w.state === 'installed' && navigator.serviceWorker.controller) ask(w); });
    if (reg.waiting && navigator.serviceWorker.controller) ask(reg.waiting);
    reg.addEventListener('updatefound', () => watch(reg.installing));
    document.addEventListener('visibilitychange', () => { if (!document.hidden) reg.update().catch(() => {}); });
    let had = !!navigator.serviceWorker.controller;
    navigator.serviceWorker.addEventListener('controllerchange', () => { if (had) location.reload(); had = true; });
  }).catch(() => { /* not a secure context, or blocked */ });
}
applyTheme();
import(`${APP.assets}mcu.js?v=1`).then((m) => { MCU = m; applyTheme(); render(); }).catch(() => { /* static fallback colours stay */ });
onRoute();
boot();
