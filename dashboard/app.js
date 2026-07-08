// WherUGo dashboard — ana uygulama (statik SPA, build adımı yok).
// Sekmeler: Genel Bakış · Isı Haritası · Bölgeler · Kuyruk · Brifing · Yönetim
// (CONTRACTS §7 + v2 §9 auth, §10 yönetim, §11 alarmlar).
// Backend yokken sayfa çökmez: her panel kendi "backend bekleniyor" durumunu gösterir
// ve mağaza bilgisi 15 sn'de bir yeniden denenir. v2 uçları olmayan eski backend'de
// (404/405) yeni paneller "backend bu özelliği henüz desteklemiyor" durumuna düşer.

import {
  apiGet, apiPost, apiPut, apiDelete, ApiError, qs,
  requestToken, setToken, clearToken, hasStoredToken, onUnauthorized,
} from './api.js';
import {
  escapeHtml, mdToHtml, fmtInt, fmtNum1, fmtPct, fmtDur,
  parseTs, fmtHour, fmtClock, fmtDay, localDateStr,
} from './format.js';
import {
  renderChart, dataTable, baseOption, axisTooltip, itemTooltip,
  catAxis, valAxis, legendStyle, rampColor,
  COLORS, SERIES, SEQ_BLUE,
} from './charts.js';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const CELL_M = 0.5; // ısı haritası hücre kenarı (metre)
const QUEUE_POLL_MS = 15000;
const BOOT_RETRY_MS = 15000;
// Heartbeat ~30 sn periyotludur; 2 dakikadan taze heartbeat = çevrimiçi kabulü.
const DEVICE_ONLINE_MS = 120000;

const state = {
  storeId: 1,
  storeName: null,
  store: null,          // {id, name, planW, planH, zones:[{id,name,type,poly}]}
  range: 'today',       // today | yesterday | last7
  tab: 'overview',
  heatKind: 'density',  // density | dwell
};

const ZONE_TYPE_TR = {
  entrance: 'Giriş', shelf: 'Reyon', queue: 'Kuyruk',
  checkout: 'Kasa', fitting_room: 'Deneme kabini', other: 'Diğer',
};

// Görünüm nesli (C11): sekme, tarih aralığı veya harita türü değişince artar.
// Her async doldurucu başlangıçta nesli yakalar ve await sonrasında nesil
// eskimişse DOM'a YAZMADAN döner — geciken eski yanıt yeni görünümü ezemez.
let renderEpoch = 0;

function bumpEpoch() {
  renderEpoch += 1;
  return renderEpoch;
}

function isStale(epoch) {
  return epoch !== renderEpoch;
}

// ---------------------------------------------------------------------------
// Yardımcılar
// ---------------------------------------------------------------------------

function currentRange() {
  const now = new Date();
  const dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  let from; let to; let days;
  if (state.range === 'yesterday') {
    to = dayStart;
    from = new Date(dayStart.getTime() - 864e5);
    days = 1;
  } else if (state.range === 'last7') {
    to = now;
    from = new Date(dayStart.getTime() - 6 * 864e5);
    days = 7;
  } else {
    from = dayStart;
    to = now;
    days = 1;
  }
  return { from, to, days, fromIso: from.toISOString(), toIso: to.toISOString() };
}

function friendly(err) {
  if (err instanceof ApiError) return err.message;
  return 'Beklenmeyen hata: ' + (err && err.message ? err.message : String(err));
}

function shortErr(err) {
  return err instanceof ApiError && err.offline ? 'Backend bekleniyor' : friendly(err);
}

function showState(el, msg) {
  if (el) el.innerHTML = `<div class="state-msg">${escapeHtml(msg)}</div>`;
}

function showError(el, err) {
  if (!el) return;
  el.innerHTML = `<div class="state-msg state-msg--err">${escapeHtml(friendly(err))}` +
    '<br><button type="button" class="btn btn--sm" data-retry>Yeniden dene</button></div>';
}

/**
 * v2 ucu eski backend'de yok: FastAPI bilinmeyen yola 404, yalnız GET tanımlı
 * yola PUT/DELETE için 405 döner. Bu durumları "hata" değil "özellik henüz yok"
 * olarak gösteririz — backend başka ajan tarafından eşzamanlı geliştiriliyor.
 */
function isV2Missing(err) {
  return err instanceof ApiError && [404, 405, 501].includes(err.status);
}

/** v2 ucu yoksa bilgilendirici durum, aksi halde normal hata gösterimi. */
function showV2State(el, err, endpointNote) {
  if (isV2Missing(err)) {
    showState(el, `Backend bu özelliği henüz desteklemiyor — ${endpointNote} ucu bekleniyor (v2).`);
  } else {
    showError(el, err);
  }
}

/** Form yanı durum mesajı ('' | 'ok' | 'err'). */
function setFormMsg(el, text, kind = '') {
  if (!el) return;
  el.textContent = text || '';
  el.className = 'form-msg' +
    (kind === 'ok' ? ' form-msg--ok' : kind === 'err' ? ' form-msg--err' : '');
}

/**
 * CONTRACTS §4 quality_detail yapılandırılmış bir NESNEDİR
 * ({window_sec, coverage_gap_sec, coverage_gap_pct, gap_count, has_data, reason}).
 * Tooltip'te '[object Object]' yerine okunur Türkçe özet üretir (m8).
 */
function qualityDetailText(detail) {
  if (detail == null) return '';
  if (typeof detail === 'string') return detail;
  if (typeof detail !== 'object') return String(detail);
  const parts = [];
  const gapSec = Number(detail.coverage_gap_sec);
  const gapPct = Number(detail.coverage_gap_pct);
  if (Number.isFinite(gapSec) && gapSec > 0) {
    parts.push(`kapsam boşluğu: ${fmtDur(gapSec)}` +
      (Number.isFinite(gapPct) ? ` (%${fmtNum1(gapPct)})` : ''));
  } else if (Number.isFinite(gapPct) && gapPct > 0) {
    parts.push(`kapsam boşluğu: %${fmtNum1(gapPct)}`);
  } else if (Number.isFinite(gapSec) || Number.isFinite(gapPct)) {
    parts.push('kapsam boşluğu yok');
  }
  const gapCount = Number(detail.gap_count);
  if (Number.isFinite(gapCount) && gapCount > 0) parts.push(`${fmtInt(gapCount)} boşluk kaydı`);
  if (typeof detail.has_data === 'boolean') parts.push(`veri: ${detail.has_data ? 'var' : 'yok'}`);
  return parts.join(' · ');
}

function qualityBadgeHtml(badge, detail, { small = false } = {}) {
  const b = ['green', 'yellow', 'red'].includes(badge) ? badge : null;
  if (!b) return '';
  const label = { green: 'Yeşil', yellow: 'Sarı', red: 'Kırmızı' }[b];
  const fallbackDetail = {
    green: 'Tam kapsam — veri kalitesi iyi.',
    yellow: 'Kapsam boşluğu penceresi %2’yi aştı; düzeltme uygulandı.',
    red: 'Veri eksik ya da kapsam boşluğu %10’u aştı.',
  }[b];
  const tip = qualityDetailText(detail) || fallbackDetail;
  return `<span class="q-badge q-badge--${b}${small ? ' q-badge--sm' : ''}" tabindex="0" ` +
    `title="${escapeHtml(tip)}"><span class="q-dot" aria-hidden="true"></span>` +
    `${small ? '' : 'Veri kalitesi: '}${label}</span>`;
}

// --- kayan araç ipucu (canvas ısı haritası için) ---------------------------

function showTip(clientX, clientY, html) {
  const tip = $('#viz-tip');
  tip.innerHTML = html;
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  const x = Math.min(clientX + 14, window.innerWidth - r.width - 8);
  const y = Math.min(clientY + 14, window.innerHeight - r.height - 8);
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}

function hideTip() {
  $('#viz-tip').hidden = true;
}

// ---------------------------------------------------------------------------
// Veri normalizasyonu — yanıt alan adlarında küçük farklara tolerans
// ---------------------------------------------------------------------------

function parsePolygon(z) {
  let p = z.polygon_json ?? z.polygon ?? z.polygon_m ?? null;
  if (typeof p === 'string') {
    try { p = JSON.parse(p); } catch { return null; }
  }
  if (p && Array.isArray(p.points)) p = p.points;
  if (!Array.isArray(p) || p.length < 3) return null;
  const pts = p
    .map((pt) => (Array.isArray(pt) ? [Number(pt[0]), Number(pt[1])] : [Number(pt?.x), Number(pt?.y)]))
    .filter((pt) => Number.isFinite(pt[0]) && Number.isFinite(pt[1]));
  return pts.length >= 3 ? pts : null;
}

function normalizeStore(d) {
  const s = d?.store ?? d ?? {};
  const zonesRaw = d?.zones ?? s.zones ?? [];
  return {
    id: s.id ?? state.storeId,
    name: s.name ?? 'Mağaza',
    planW: Number(s.plan_width_m) > 0 ? Number(s.plan_width_m) : 20,
    planH: Number(s.plan_height_m) > 0 ? Number(s.plan_height_m) : 12,
    timezone: typeof s.timezone === 'string' ? s.timezone : '',
    webhookUrl: typeof s.webhook_url === 'string' ? s.webhook_url : '',
    zones: (Array.isArray(zonesRaw) ? zonesRaw : []).map((z) => ({
      id: z.id,
      name: z.name ?? `Bölge ${z.id}`,
      type: String(z.zone_type ?? z.type ?? 'other'),
      category: z.category == null ? '' : String(z.category),
      poly: parsePolygon(z),
    })),
  };
}

async function ensureStore() {
  if (state.store) return state.store;
  const detail = await apiGet(`/v1/stores/${state.storeId}`);
  state.store = normalizeStore(detail);
  return state.store;
}

/**
 * Mağaza detayını zorla yeniden çeker (yönetim mutasyonlarından sonra):
 * ısı haritası ve diğer sekmeler yeni/silinen bölgeleri hemen görür (§10).
 */
async function refreshStore() {
  state.store = null;
  const store = await ensureStore();
  state.storeName = store.name;
  const nameEl = $('#store-name');
  if (nameEl) nameEl.textContent = store.name;
  return store;
}

function zoneNameById(id) {
  const z = state.store?.zones.find((x) => String(x.id) === String(id));
  return z ? z.name : `Bölge ${id}`;
}

/** CONTRACTS §4: metrics → {series:[{ts,value}], quality_badge, quality_detail}. */
async function fetchMetric(metric, granularity, r) {
  const d = await apiGet(`/v1/stores/${state.storeId}/metrics` + qs({
    metric, granularity, from: r.fromIso, to: r.toIso,
  }));
  const series = (Array.isArray(d?.series) ? d.series : [])
    .map((pt) => ({ ts: parseTs(pt.ts ?? pt.time ?? pt.t), value: Number(pt.value ?? pt.v ?? 0) }))
    .filter((pt) => pt.ts && Number.isFinite(pt.value));
  return { series, badge: d?.quality_badge ?? null, detail: d?.quality_detail ?? '' };
}

function normalizeDwell(d) {
  const stats = d?.stats ?? d?.dwell ?? d ?? {};
  const num = (v) => (v == null || !Number.isFinite(Number(v)) ? null : Number(v));
  return {
    p50: num(stats.p50 ?? stats.p50_sec ?? stats.dwell_p50),
    p95: num(stats.p95 ?? stats.p95_sec ?? stats.dwell_p95),
    visits: num(d?.visits ?? d?.visit_count ?? d?.n_visits ?? stats.visits),
    draw: num(d?.draw_rate ?? d?.drawRate ?? stats.draw_rate),
  };
}

function normalizeDistribution(d) {
  let items = d;
  if (d && !Array.isArray(d)) {
    items = d.distribution ?? d.items ?? d.first_destinations ?? d.zones ?? null;
  }
  if (items && !Array.isArray(items) && typeof items === 'object') {
    return Object.entries(items).map(([name, v]) => ({ name, value: Number(v) || 0 }));
  }
  if (!Array.isArray(items)) return null;
  return items
    .map((it) => ({
      name: it.zone_name ?? it.zone ?? it.name
        ?? (it.zone_id != null ? zoneNameById(it.zone_id) : '—'),
      value: Number(it.count ?? it.value ?? it.visits ?? it.share ?? 0),
    }))
    .filter((it) => Number.isFinite(it.value));
}

function normalizeTransitions(d) {
  if (!d) return null;
  const fromPairs = (arr) => {
    const names = [];
    const seen = new Map();
    const idx = (n) => {
      if (!seen.has(n)) { seen.set(n, names.length); names.push(n); }
      return seen.get(n);
    };
    const triples = [];
    for (const it of arr) {
      const f = it.from_zone ?? it.from ?? it.source ?? it.src;
      const t = it.to_zone ?? it.to ?? it.target ?? it.dst;
      const c = Number(it.count ?? it.value ?? it.n ?? 0);
      if (f == null || t == null) continue;
      const fn = typeof f === 'number' ? zoneNameById(f) : String(f);
      const tn = typeof t === 'number' ? zoneNameById(t) : String(t);
      triples.push([idx(fn), idx(tn), c]);
    }
    if (!names.length) return null;
    const m = names.map(() => names.map(() => null));
    for (const [i, j, c] of triples) m[i][j] = (m[i][j] ?? 0) + c;
    return { labels: names, matrix: m };
  };

  if (Array.isArray(d)) return fromPairs(d);
  if (Array.isArray(d.transitions)) return fromPairs(d.transitions);
  if (Array.isArray(d.matrix)) {
    const labels = (d.zones ?? d.labels ?? []).map((z) =>
      (typeof z === 'object' ? (z.name ?? zoneNameById(z.id)) : (typeof z === 'number' ? zoneNameById(z) : String(z))));
    if (labels.length && Array.isArray(d.matrix[0])) {
      return { labels, matrix: d.matrix.map((row) => row.map((v) => (v == null ? null : Number(v)))) };
    }
    return null;
  }
  const dict = d.matrix && typeof d.matrix === 'object' ? d.matrix : d;
  const keys = Object.keys(dict).filter((k) => dict[k] && typeof dict[k] === 'object');
  if (keys.length) {
    const cols = new Set();
    keys.forEach((k) => Object.keys(dict[k]).forEach((c) => cols.add(c)));
    const labels = [...new Set([...keys, ...cols])];
    const nameOf = (k) => (/^\d+$/.test(k) ? zoneNameById(k) : k);
    const m = labels.map((f) => labels.map((t) => {
      const v = dict[f]?.[t];
      return v == null ? null : Number(v);
    }));
    return { labels: labels.map(nameOf), matrix: m };
  }
  return null;
}

function normalizeQueueLive(d) {
  const num = (v) => (v == null || !Number.isFinite(Number(v)) ? null : Number(v));
  const pickLen = (o) => num(o?.queue_len ?? o?.len ?? o?.length ?? o?.value);
  const pickWait = (o) => num(o?.est_wait_sec ?? o?.wait_sec ?? o?.est_wait ?? o?.wait);
  const last = d?.last ?? d?.latest ?? d?.current ?? d?.last_measurement ?? d ?? {};
  const seriesRaw = d?.series ?? d?.history ?? d?.samples ?? d?.last_hour ?? [];
  const series = (Array.isArray(seriesRaw) ? seriesRaw : [])
    .map((p) => ({ ts: parseTs(p.ts ?? p.time ?? p.timestamp), len: pickLen(p), wait: pickWait(p) }))
    .filter((p) => p.ts && p.len != null);
  return {
    len: pickLen(last),
    wait: pickWait(last),
    active: num(last?.active_checkouts ?? last?.checkouts),
    ts: parseTs(last?.ts ?? last?.time ?? last?.timestamp),
    series,
    alert: Boolean(d?.alert),
  };
}

// ---------------------------------------------------------------------------
// Üst bar, sekmeler, bağlantı durumu
// ---------------------------------------------------------------------------

let bootRetryTimer = null;

function setBanner(msg) {
  const el = $('#conn-banner');
  if (!msg) { el.hidden = true; el.textContent = ''; return; }
  el.hidden = false;
  el.textContent = msg;
}

// --- token/oturum (CONTRACTS §9) --------------------------------------------
// Varsayılan token 'demo'dur (api.js). Herhangi bir istek 401 dönerse panel
// açılır; API anahtarı POST /v1/auth/token ile token'a çevrilir ve
// localStorage'a yazılır. Çıkış, kayıtlı token'ı siler → 'demo'ya dönülür.

function showAuthPanel() {
  const ov = $('#auth-overlay');
  if (!ov || !ov.hidden) return; // eşzamanlı 401'lerde tek panel
  const errEl = $('#auth-err');
  if (errEl) { errEl.hidden = true; errEl.textContent = ''; }
  ov.hidden = false;
  const input = $('#auth-key');
  if (input) { input.value = ''; setTimeout(() => input.focus(), 0); }
}

function hideAuthPanel() {
  const ov = $('#auth-overlay');
  if (ov) ov.hidden = true;
}

function setAuthPanelError(msg) {
  const el = $('#auth-err');
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
}

async function onAuthSubmit(e) {
  e.preventDefault();
  const key = $('#auth-key').value.trim();
  if (!key) return;
  const btn = $('#auth-submit');
  btn.disabled = true;
  btn.textContent = 'Doğrulanıyor…';
  try {
    const d = await requestToken(key); // {token, expires_in}
    setToken(d.token);
    hideAuthPanel();
    updateLogoutButton();
    state.store = null; // yeni kimlikle mağaza yeniden çekilsin
    await bootstrap(false);
    renderCurrent();
  } catch (err) {
    if (isV2Missing(err)) {
      setAuthPanelError('Backend token ucunu henüz desteklemiyor (POST /v1/auth/token — v2 bekleniyor).');
    } else if (err instanceof ApiError && err.status === 401) {
      setAuthPanelError('API anahtarı reddedildi — lütfen kontrol edip yeniden deneyin.');
    } else {
      setAuthPanelError(friendly(err));
    }
  } finally {
    btn.disabled = false;
    btn.textContent = 'Giriş yap';
  }
}

function onLogout() {
  clearToken(); // kayıtlı token silinir; api.js 'demo'ya döner (§9)
  updateLogoutButton();
  hideAuthPanel();
  state.store = null;
  bootstrap(false).finally(() => renderCurrent());
}

function updateLogoutButton() {
  const btn = $('#btn-logout');
  if (btn) btn.hidden = !hasStoredToken();
}

function wireAuth() {
  onUnauthorized(() => showAuthPanel());
  const form = $('#auth-form');
  if (form) form.addEventListener('submit', onAuthSubmit);
  const cancel = $('#auth-cancel');
  if (cancel) cancel.addEventListener('click', hideAuthPanel);
  const logout = $('#btn-logout');
  if (logout) logout.addEventListener('click', onLogout);
  updateLogoutButton();
}

async function bootstrap(rerenderOnSuccess = false) {
  try {
    const data = await apiGet('/v1/stores');
    const list = Array.isArray(data) ? data : (data?.stores ?? data?.items ?? []);
    const first = list[0];
    if (first) {
      state.storeId = first.id ?? state.storeId;
      state.storeName = first.name ?? 'Mağaza';
    }
    const detail = await apiGet(`/v1/stores/${state.storeId}`);
    state.store = normalizeStore(detail);
    if (!state.storeName) state.storeName = state.store.name;
    $('#store-name').textContent = state.storeName;
    setBanner(null);
    if (bootRetryTimer) { clearInterval(bootRetryTimer); bootRetryTimer = null; }
    if (rerenderOnSuccess) renderCurrent();
  } catch (err) {
    $('#store-name').textContent = 'Mağaza bekleniyor…';
    setBanner(friendly(err) + ' Bağlantı 15 saniyede bir yeniden denenecek.');
    if (!bootRetryTimer) bootRetryTimer = setInterval(() => bootstrap(true), BOOT_RETRY_MS);
  }
}

function wireTopbar() {
  $$('.seg-btn[data-range]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (state.range === btn.dataset.range) return;
      state.range = btn.dataset.range;
      $$('.seg-btn[data-range]').forEach((b) => b.classList.toggle('is-active', b === btn));
      renderCurrent();
    });
  });
  $$('.tab-btn[data-tab]').forEach((btn) => {
    btn.addEventListener('click', () => setTab(btn.dataset.tab));
  });
  // Panel içi "Yeniden dene" düğmeleri için tek delegasyon.
  $('#view').addEventListener('click', (e) => {
    if (e.target.closest('[data-retry]')) renderCurrent();
  });
}

function setTab(tab) {
  if (state.tab === tab) return;
  state.tab = tab;
  $$('.tab-btn[data-tab]').forEach((b) => {
    const active = b.dataset.tab === tab;
    b.classList.toggle('is-active', active);
    b.setAttribute('aria-selected', String(active));
  });
  renderCurrent();
}

function renderCurrent() {
  bumpEpoch(); // uçuştaki eski yanıtlar artık DOM'a yazamaz (C11)
  stopQueuePolling();
  hideTip();
  const root = $('#view');
  const views = {
    overview: viewOverview,
    heatmap: viewHeatmap,
    zones: viewZones,
    queue: viewQueue,
    briefing: viewBriefing,
    admin: viewAdmin,
  };
  (views[state.tab] || viewOverview)(root);
}

// ---------------------------------------------------------------------------
// KPI kartları
// ---------------------------------------------------------------------------

function kpiTile(id, label) {
  return `<section class="panel kpi" id="${id}">
    <div class="kpi-top"><span class="kpi-label">${escapeHtml(label)}</span><span class="kpi-badge"></span></div>
    <div class="kpi-value">–</div>
    <div class="kpi-sub">Yükleniyor…</div>
  </section>`;
}

function setKpi(id, value, sub, badgeHtml = '') {
  const el = document.getElementById(id);
  if (!el) return;
  el.querySelector('.kpi-value').textContent = value;
  el.querySelector('.kpi-sub').textContent = sub || '';
  el.querySelector('.kpi-badge').innerHTML = badgeHtml;
}

function setKpiError(id, err) {
  setKpi(id, '—', shortErr(err));
}

// ---------------------------------------------------------------------------
// 1) Genel Bakış
// ---------------------------------------------------------------------------

async function viewOverview(root) {
  const epoch = renderEpoch;
  root.innerHTML = `
    <div class="kpi-row">
      ${kpiTile('kpi-ff', 'Ziyaretçi (footfall)')}
      ${kpiTile('kpi-occ', 'Anlık doluluk')}
      ${kpiTile('kpi-conv', 'Dönüşüm oranı')}
    </div>
    <section class="panel">
      <div class="panel-head">
        <h2 id="ov-title">Saatlik ziyaretçi (footfall)</h2>
        <span id="ov-badge"></span>
      </div>
      <div id="ov-chart" class="chart chart--tall"><div class="state-msg">Yükleniyor…</div></div>
    </section>`;

  const r = currentRange();
  const gran = r.days > 1 ? '1d' : '1h';
  $('#ov-title').textContent = gran === '1h'
    ? 'Saatlik ziyaretçi (footfall)' : 'Günlük ziyaretçi (footfall)';

  const [ff, occ, conv] = await Promise.allSettled([
    fetchMetric('footfall', gran, r),
    fetchMetric('occupancy', '1h', r),
    fetchMetric('conversion', '1d', r),
  ]);
  if (isStale(epoch)) return; // görünüm değişti — eski yanıtı çizme (C11)

  // Footfall kartı + grafik
  if (ff.status === 'fulfilled') {
    const m = ff.value;
    const total = m.series.reduce((a, p) => a + p.value, 0);
    setKpi('kpi-ff', fmtInt(total), 'dönem toplamı · personel hariç',
      qualityBadgeHtml(m.badge, m.detail, { small: true }));
    $('#ov-badge').innerHTML = qualityBadgeHtml(m.badge, m.detail);
    fillFootfallChart($('#ov-chart'), m, gran);
  } else {
    setKpiError('kpi-ff', ff.reason);
    showError($('#ov-chart'), ff.reason);
  }

  // Doluluk kartı
  if (occ.status === 'fulfilled') {
    const s = occ.value.series;
    const lastPt = s[s.length - 1];
    const peak = s.length ? Math.max(...s.map((p) => p.value)) : null;
    setKpi('kpi-occ',
      lastPt ? fmtInt(lastPt.value) + ' kişi' : '—',
      lastPt ? `son ölçüm ${fmtClock(lastPt.ts)} · tepe ${fmtInt(peak)}` : 'bu aralıkta veri yok',
      qualityBadgeHtml(occ.value.badge, occ.value.detail, { small: true }));
  } else {
    setKpiError('kpi-occ', occ.reason);
  }

  // Dönüşüm kartı
  if (conv.status === 'fulfilled') {
    const s = conv.value.series;
    const lastPt = s[s.length - 1];
    setKpi('kpi-conv',
      lastPt ? fmtPct(lastPt.value) : '—',
      lastPt ? 'POS işlemleri ÷ ziyaretçi' : 'bu aralıkta veri yok (POS import gerekli)',
      qualityBadgeHtml(conv.value.badge, conv.value.detail, { small: true }));
  } else {
    setKpiError('kpi-conv', conv.reason);
  }
}

function fillFootfallChart(el, m, gran) {
  if (!m.series.length) {
    showState(el, 'Bu aralıkta footfall verisi yok.');
    return;
  }
  const label = (p) => (gran === '1h' ? fmtHour(p.ts) : fmtDay(p.ts));
  const labels = m.series.map(label);
  renderChart(el, baseOption({
    xAxis: catAxis(labels),
    yAxis: valAxis(),
    series: [{
      name: 'Ziyaretçi',
      type: 'bar',
      data: m.series.map((p) => p.value),
      itemStyle: { color: COLORS.blue, borderRadius: [4, 4, 0, 0] },
      barMaxWidth: 22,
      barCategoryGap: '30%',
    }],
  }), {
    caption: 'Ziyaretçi (footfall)',
    columns: [gran === '1h' ? 'Saat' : 'Gün', 'Ziyaretçi'],
    rows: m.series.map((p) => [label(p), fmtInt(p.value)]),
  });
}

// ---------------------------------------------------------------------------
// 2) Isı Haritası
// ---------------------------------------------------------------------------

async function viewHeatmap(root) {
  root.innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>Isı haritası — zemin planı</h2>
        <div class="seg" role="group" aria-label="Harita türü">
          <button type="button" class="seg-btn${state.heatKind === 'density' ? ' is-active' : ''}" data-kind="density">Yoğunluk</button>
          <button type="button" class="seg-btn${state.heatKind === 'dwell' ? ' is-active' : ''}" data-kind="dwell">Dwell</button>
        </div>
      </div>
      <div id="heat-wrap" class="heat-wrap"><div class="state-msg">Yükleniyor…</div></div>
      <div id="heat-meta" class="heat-meta"></div>
    </section>`;

  $$('.seg-btn[data-kind]', root).forEach((btn) => {
    btn.addEventListener('click', () => {
      if (state.heatKind === btn.dataset.kind) return;
      state.heatKind = btn.dataset.kind;
      bumpEpoch(); // eski tür için uçuştaki yanıt çizilmesin (C11)
      $$('.seg-btn[data-kind]', root).forEach((b) => b.classList.toggle('is-active', b === btn));
      fillHeatmap();
    });
  });

  fillHeatmap();
}

async function fillHeatmap() {
  const epoch = renderEpoch;
  const kind = state.heatKind; // yanıt bu türe ait — çizim etiketi de öyle olmalı
  const wrap = $('#heat-wrap');
  const meta = $('#heat-meta');
  if (!wrap) return;
  try {
    const store = await ensureStore();
    const r = currentRange();
    const d = await apiGet(`/v1/stores/${state.storeId}/heatmap` + qs({
      from: r.fromIso, to: r.toIso, cell_m: CELL_M, kind,
    }));
    if (isStale(epoch)) return; // tür/aralık/sekme değişti (C11)
    const cells = normalizeHeatCells(Array.isArray(d?.cells) ? d.cells : [], store);
    drawHeatmap(wrap, meta, store, cells, Number(d?.k_suppressed) || 0, kind);
  } catch (err) {
    if (isStale(epoch)) return;
    showError(wrap, err);
    if (meta) meta.innerHTML = '';
  }
}

/**
 * Hücre koordinatı sözleşmede {x,y} olarak geçer; metre mi hücre indeksi mi
 * belirtilmemiştir. Kesirli değer varsa metre kabul edilir; aksi halde
 * (tam sayılar) hücre indeksi varsayılıp cell_m ile çarpılır.
 */
function normalizeHeatCells(cellsRaw, store) {
  const cells = cellsRaw
    .map((c) => ({ x: Number(c.x), y: Number(c.y), value: Number(c.value ?? c.v ?? c.count ?? 0) }))
    .filter((c) => Number.isFinite(c.x) && Number.isFinite(c.y) && Number.isFinite(c.value) && c.value > 0);
  if (!cells.length) return [];
  const fractional = cells.some((c) => !Number.isInteger(c.x) || !Number.isInteger(c.y));
  const asIndex = !fractional;
  return cells
    .map((c) => ({
      mx: asIndex ? c.x * CELL_M : c.x,
      my: asIndex ? c.y * CELL_M : c.y,
      value: c.value,
    }))
    .filter((c) => c.mx >= 0 && c.my >= 0 && c.mx < store.planW && c.my < store.planH);
}

function drawHeatmap(wrap, meta, store, cells, kSuppressed, kind = state.heatKind) {
  wrap.innerHTML = '';
  const canvas = document.createElement('canvas');
  canvas.className = 'heat-canvas';
  wrap.appendChild(canvas);

  const cssW = Math.max(320, Math.min(wrap.clientWidth || 880, 960));
  const scale = cssW / store.planW;
  const cssH = Math.round(store.planH * scale);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  canvas.style.width = cssW + 'px';
  canvas.style.height = cssH + 'px';

  const ctx = canvas.getContext('2d');
  if (!ctx) { // canvas yoksa (çok eski tarayıcı) tablo fallback'i
    wrap.innerHTML = dataTable({
      caption: 'Isı haritası hücreleri',
      columns: ['x (m)', 'y (m)', 'Değer'],
      rows: cells.map((c) => [fmtNum1(c.mx), fmtNum1(c.my), fmtNum1(c.value)]),
      numFrom: 0,
    });
    return;
  }
  ctx.scale(dpr, dpr);

  // Zemin + metre gridi (5 m aralık, sessiz çizgiler)
  ctx.fillStyle = '#f6f5f1';
  ctx.fillRect(0, 0, cssW, cssH);
  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 1;
  for (let gx = 5; gx < store.planW; gx += 5) {
    ctx.beginPath(); ctx.moveTo(gx * scale + 0.5, 0); ctx.lineTo(gx * scale + 0.5, cssH); ctx.stroke();
  }
  for (let gy = 5; gy < store.planH; gy += 5) {
    ctx.beginPath(); ctx.moveTo(0, gy * scale + 0.5); ctx.lineTo(cssW, gy * scale + 0.5); ctx.stroke();
  }

  // Hücreler (karekök ölçek: yoğunluk dağılımı ağır kuyrukludur)
  const maxV = cells.length ? Math.max(...cells.map((c) => c.value)) : 0;
  const cellPx = CELL_M * scale;
  const index = new Map();
  for (const c of cells) {
    const t = maxV > 0 ? Math.sqrt(c.value / maxV) : 0;
    ctx.fillStyle = rampColor(t);
    ctx.fillRect(c.mx * scale + 0.5, c.my * scale + 0.5,
      Math.max(1, cellPx - 1), Math.max(1, cellPx - 1));
    index.set(`${Math.round(c.mx / CELL_M)}|${Math.round(c.my / CELL_M)}`, c.value);
  }

  // Zone poligonları + etiketler (beyaz haleli, okunur)
  ctx.font = `11px ${'system-ui, sans-serif'}`;
  for (const z of store.zones) {
    if (!z.poly) continue;
    ctx.beginPath();
    z.poly.forEach(([px, py], i) => {
      if (i) ctx.lineTo(px * scale, py * scale); else ctx.moveTo(px * scale, py * scale);
    });
    ctx.closePath();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = 'rgba(11,11,11,0.45)';
    ctx.stroke();

    const cx = z.poly.reduce((a, p) => a + p[0], 0) / z.poly.length * scale;
    const cy = z.poly.reduce((a, p) => a + p[1], 0) / z.poly.length * scale;
    const w = ctx.measureText(z.name).width;
    ctx.lineWidth = 3;
    ctx.strokeStyle = 'rgba(252,252,251,0.9)';
    ctx.strokeText(z.name, cx - w / 2, cy + 4);
    ctx.fillStyle = COLORS.inkSecondary;
    ctx.fillText(z.name, cx - w / 2, cy + 4);
  }

  // Çerçeve
  ctx.strokeStyle = COLORS.axis;
  ctx.lineWidth = 1;
  ctx.strokeRect(0.5, 0.5, cssW - 1, cssH - 1);

  // Hücre üstünde araç ipucu — birim, verinin alındığı türe göre (C11)
  const unit = kind === 'density' ? 'geçiş' : 'sn dwell';
  canvas.addEventListener('mousemove', (e) => {
    const rect = canvas.getBoundingClientRect();
    const mx = (e.clientX - rect.left) / scale;
    const my = (e.clientY - rect.top) / scale;
    const ix = Math.floor(mx / CELL_M);
    const iy = Math.floor(my / CELL_M);
    const v = index.get(`${ix}|${iy}`);
    if (v == null) { hideTip(); return; }
    showTip(e.clientX, e.clientY,
      `<strong>${escapeHtml(fmtNum1(v))}</strong> ${unit}` +
      `<br><span class="tip-sub">x ${escapeHtml(fmtNum1(ix * CELL_M))}–${escapeHtml(fmtNum1((ix + 1) * CELL_M))} m · ` +
      `y ${escapeHtml(fmtNum1(iy * CELL_M))}–${escapeHtml(fmtNum1((iy + 1) * CELL_M))} m</span>`);
  });
  canvas.addEventListener('mouseleave', hideTip);

  // Gösterge + KVKK notu
  const kindNote = kind === 'density'
    ? 'Yoğunluk: hücreden geçen konum örneği sayısı.'
    : 'Dwell: hücrede duraklamayla geçirilen toplam süre (sn).';
  const kNote = kSuppressed > 0
    ? `${fmtInt(kSuppressed)} hücre k&lt;10 anonimlik eşiğinin altında kaldığı için gizlendi.`
    : 'k&lt;10 bastırma aktif — bu aralıkta bastırılan hücre yok.';
  meta.innerHTML = `
    <div class="heat-legend" aria-hidden="true">
      <span>0</span><span class="heat-grad"></span><span>${escapeHtml(fmtNum1(maxV))} ${unit}</span>
    </div>
    <p class="heat-note">${escapeHtml(kindNote)} ${kNote} Hücre boyutu ${CELL_M} m, renk ölçeği karekök. ` +
    `Plan ${escapeHtml(fmtNum1(store.planW))} × ${escapeHtml(fmtNum1(store.planH))} m${
      cells.length ? '' : ' — bu aralıkta hücre verisi yok'}.</p>`;
}

// ---------------------------------------------------------------------------
// 3) Bölgeler
// ---------------------------------------------------------------------------

async function viewZones(root) {
  root.innerHTML = `
    <section class="panel">
      <div class="panel-head"><h2>Bölge dwell süreleri (p50 / p95)</h2></div>
      <div id="z-dwell" class="chart chart--tall"><div class="state-msg">Yükleniyor…</div></div>
    </section>
    <div class="grid-2">
      <section class="panel">
        <div class="panel-head"><h2>İlk gidilen bölge</h2><span class="panel-note" id="z-first-note"></span></div>
        <div id="z-first" class="chart"><div class="state-msg">Yükleniyor…</div></div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>Bölge özeti</h2><span class="panel-note">draw rate = duran ÷ önünden geçen</span></div>
        <div id="z-table"><div class="state-msg">Yükleniyor…</div></div>
      </section>
    </div>
    <section class="panel">
      <div class="panel-head"><h2>Bölge geçiş matrisi</h2><span class="panel-note">k&lt;10 geçişler bastırılır — satır: nereden, sütun: nereye</span></div>
      <div id="z-trans"><div class="state-msg">Yükleniyor…</div></div>
    </section>`;

  const r = currentRange();
  fillZoneDwell(r);
  fillFirstDestination(r);
  fillTransitions(r);
}

async function fillZoneDwell(r) {
  const epoch = renderEpoch;
  const chartEl = $('#z-dwell');
  const tableEl = $('#z-table');
  try {
    const store = await ensureStore();
    if (isStale(epoch)) return; // görünüm değişti (C11)
    if (!store.zones.length) {
      showState(chartEl, 'Bu mağazada bölge tanımı yok.');
      showState(tableEl, 'Bu mağazada bölge tanımı yok.');
      return;
    }
    const results = await Promise.allSettled(store.zones.map((z) =>
      apiGet(`/v1/stores/${state.storeId}/zones/${z.id}/dwell` + qs({
        stat: 'p50,p95', from: r.fromIso, to: r.toIso,
      }))));
    if (isStale(epoch)) return; // aralık/sekme değişti — eski yanıtları çizme (C11)
    const rows = store.zones.map((z, i) => (results[i].status === 'fulfilled'
      ? { zone: z, ...normalizeDwell(results[i].value) }
      : { zone: z, error: results[i].reason }));
    const ok = rows.filter((x) => !x.error);
    if (!ok.length) throw rows[0].error ?? new ApiError('Dwell verisi alınamadı.');

    const sorted = [...ok].sort((a, b) => (b.p95 ?? 0) - (a.p95 ?? 0));
    const names = sorted.map((x) => x.zone.name);
    renderChart(chartEl, baseOption({
      legend: legendStyle({ data: ['p50 (medyan)', 'p95'] }),
      grid: { left: 12, right: 18, top: 52, bottom: 10, containLabel: true },
      xAxis: catAxis(names, {
        axisLabel: { color: COLORS.muted, fontSize: 11, rotate: names.length > 6 ? 24 : 0, interval: 0 },
      }),
      yAxis: valAxis({ name: 'sn' }),
      series: [
        {
          name: 'p50 (medyan)', type: 'bar', data: sorted.map((x) => x.p50),
          itemStyle: { color: COLORS.blue, borderRadius: [4, 4, 0, 0] }, barMaxWidth: 18,
        },
        {
          name: 'p95', type: 'bar', data: sorted.map((x) => x.p95),
          itemStyle: { color: COLORS.blueLight, borderRadius: [4, 4, 0, 0] }, barMaxWidth: 18,
        },
      ],
    }), {
      caption: 'Bölge dwell süreleri',
      columns: ['Bölge', 'p50', 'p95'],
      rows: sorted.map((x) => [x.zone.name, fmtDur(x.p50), fmtDur(x.p95)]),
    });

    tableEl.innerHTML = dataTable({
      columns: ['Bölge', 'Tip', 'Ziyaret', 'Dwell p50', 'Dwell p95', 'Draw rate'],
      rows: rows.map((x) => (x.error
        ? [x.zone.name, ZONE_TYPE_TR[x.zone.type] ?? x.zone.type, '—', '—', '—', '—']
        : [x.zone.name, ZONE_TYPE_TR[x.zone.type] ?? x.zone.type,
          fmtInt(x.visits), fmtDur(x.p50), fmtDur(x.p95), fmtPct(x.draw)])),
      numFrom: 2,
    });
  } catch (err) {
    if (isStale(epoch)) return;
    showError(chartEl, err);
    showError(tableEl, err);
  }
}

async function fillFirstDestination(r) {
  const epoch = renderEpoch;
  const el = $('#z-first');
  try {
    await ensureStore(); // zone_id → ad çevirisi için
    const d = await apiGet(`/v1/stores/${state.storeId}/paths/first-destination` + qs({
      from: r.fromIso, to: r.toIso,
    }));
    if (isStale(epoch)) return; // görünüm değişti (C11)
    const items = (normalizeDistribution(d) ?? []).filter((it) => it.value > 0);
    if (!items.length) {
      showState(el, 'Bu aralıkta ilk-varış verisi yok.');
      return;
    }
    items.sort((a, b) => b.value - a.value);
    const top = items.slice(0, 7);
    const restSum = items.slice(7).reduce((a, it) => a + it.value, 0);
    if (restSum > 0) top.push({ name: 'Diğer', value: restSum, other: true });

    renderChart(el, baseOption({
      tooltip: itemTooltip({ formatter: '{b}: {c} ({d}%)' }),
      legend: legendStyle({ top: undefined, bottom: 0, type: 'scroll' }),
      series: [{
        type: 'pie',
        radius: ['42%', '68%'],
        center: ['50%', '44%'],
        data: top.map((it, i) => ({
          name: it.name,
          value: it.value,
          itemStyle: { color: it.other ? COLORS.other : SERIES[i % SERIES.length] },
        })),
        label: { color: COLORS.inkSecondary, fontSize: 11, formatter: '{b}\n{d}%' },
        labelLine: { lineStyle: { color: COLORS.axis } },
        itemStyle: { borderColor: COLORS.surface, borderWidth: 2 },
      }],
    }), {
      caption: 'İlk gidilen bölge dağılımı',
      columns: ['Bölge', 'Ziyaret'],
      rows: top.map((it) => [it.name, fmtInt(it.value)]),
    });
    const kSup = Number(d?.k_suppressed) || 0;
    $('#z-first-note').textContent = kSup > 0 ? `${fmtInt(kSup)} kayıt k<10 bastırıldı` : '';
  } catch (err) {
    if (isStale(epoch)) return;
    showError(el, err);
  }
}

async function fillTransitions(r) {
  const epoch = renderEpoch;
  const el = $('#z-trans');
  try {
    await ensureStore();
    const d = await apiGet(`/v1/stores/${state.storeId}/paths/transitions` + qs({
      from: r.fromIso, to: r.toIso,
    }));
    if (isStale(epoch)) return; // görünüm değişti (C11)
    const t = normalizeTransitions(d);
    if (!t || !t.labels.length) {
      showState(el, 'Bu aralıkta geçiş verisi yok.');
      return;
    }
    el.innerHTML = transitionsMatrixHtml(t.labels, t.matrix);
  } catch (err) {
    if (isStale(epoch)) return;
    showError(el, err);
  }
}

function transitionsMatrixHtml(labels, matrix) {
  const flat = matrix.flat().filter((v) => Number.isFinite(v));
  const max = flat.length ? Math.max(...flat) : 0;
  let html = '<div class="table-scroll"><table class="data-table matrix"><thead><tr>' +
    '<th scope="col">Nereden \\ Nereye</th>' +
    labels.map((l) => `<th scope="col">${escapeHtml(l)}</th>`).join('') +
    '</tr></thead><tbody>';
  matrix.forEach((row, i) => {
    html += `<tr><th scope="row">${escapeHtml(labels[i])}</th>` + row.map((v) => {
      if (v == null) return '<td class="num muted">—</td>';
      const a = max > 0 && v > 0 ? 0.32 * Math.sqrt(v / max) : 0;
      return `<td class="num" style="background:rgba(42,120,214,${a.toFixed(3)})">${escapeHtml(fmtInt(v))}</td>`;
    }).join('') + '</tr>';
  });
  return html + '</tbody></table></div>';
}

// ---------------------------------------------------------------------------
// 4) Kuyruk (15 sn'de bir canlı yenileme)
// ---------------------------------------------------------------------------

let queueTimer = null;
let queueInFlight = 0; // m5: uçuşta istek varken yeni poll tick'i başlatılmaz

function startQueuePolling() {
  stopQueuePolling();
  queueTimer = setInterval(() => {
    // Önceki istek hâlâ uçuştaysa bu tick atlanır: istekler üst üste binemez
    // ve yavaş yanıt, sonraki tick'in taze verisini ezemez (m5).
    if (state.tab === 'queue' && queueInFlight === 0) fillQueue(false);
  }, QUEUE_POLL_MS);
}

function stopQueuePolling() {
  if (queueTimer) { clearInterval(queueTimer); queueTimer = null; }
}

function pickQueueZone(store) {
  return store.zones.find((z) => z.type.toLowerCase() === 'queue')
    ?? store.zones.find((z) => /kuyruk|queue/i.test(z.name))
    ?? null;
}

async function viewQueue(root) {
  const epoch = renderEpoch;
  root.innerHTML = `
    <div id="q-alert" aria-live="polite"></div>
    <div class="kpi-row">
      ${kpiTile('kpi-qlen', 'Kuyruk uzunluğu')}
      ${kpiTile('kpi-qwait', 'Tahmini bekleme')}
      ${kpiTile('kpi-qcheck', 'Aktif kasa')}
    </div>
    <section class="panel">
      <div class="panel-head">
        <h2>Son 1 saat — kuyruk uzunluğu</h2>
        <span class="panel-note" id="q-upd"></span>
      </div>
      <div id="q-chart" class="chart chart--tall"><div class="state-msg">Yükleniyor…</div></div>
    </section>
    <section class="panel">
      <div class="panel-head">
        <h2>Alarm geçmişi</h2>
        <span class="panel-note">eşik aşağıdan yukarı aşılınca kayıt açılır (§11) · webhook teslimi ✓/✗</span>
      </div>
      <div id="al-body"><div class="state-msg">Yükleniyor…</div></div>
    </section>`;

  fillAlerts(currentRange()); // alarm geçmişi seçili tarih aralığını izler
  await fillQueue(true);
  if (isStale(epoch)) return; // kullanıcı bu arada sekme değiştirdi (C11)
  startQueuePolling();
}

// --- alarm geçmişi (CONTRACTS §11) -------------------------------------------

function normalizeAlerts(d) {
  const list = Array.isArray(d) ? d : (d?.alerts ?? d?.items ?? []);
  if (!Array.isArray(list)) return [];
  const num = (v) => (v == null || !Number.isFinite(Number(v)) ? null : Number(v));
  return list
    .map((a) => {
      let payload = a?.payload_json ?? a?.payload ?? {};
      if (typeof payload === 'string') {
        try { payload = JSON.parse(payload); } catch { payload = {}; }
      }
      if (payload == null || typeof payload !== 'object') payload = {};
      const deliveredRaw = a?.delivered ?? a?.delivered_bool ?? null;
      return {
        ts: parseTs(a?.ts ?? a?.time ?? a?.created_at ?? payload.ts),
        type: String(a?.type ?? payload.type ?? ''),
        zoneId: a?.zone_id ?? payload.zone_id ?? null,
        len: num(a?.queue_len ?? payload.queue_len),
        wait: num(a?.est_wait_sec ?? payload.est_wait_sec),
        delivered: deliveredRaw == null ? null : Boolean(deliveredRaw),
      };
    })
    .filter((a) => a.ts);
}

async function fillAlerts(r) {
  const epoch = renderEpoch;
  const el = $('#al-body');
  if (!el) return;
  try {
    await ensureStore(); // zone_id → ad çevirisi için
    const d = await apiGet(`/v1/stores/${state.storeId}/alerts` + qs({
      from: r.fromIso, to: r.toIso,
    }));
    if (isStale(epoch)) return; // görünüm değişti (C11)
    const alerts = normalizeAlerts(d).sort((a, b) => b.ts - a.ts);
    if (!alerts.length) {
      showState(el, 'Bu aralıkta alarm kaydı yok.');
      return;
    }
    el.innerHTML = alertsTableHtml(alerts);
  } catch (err) {
    if (isStale(epoch)) return;
    showV2State(el, err, 'GET /v1/stores/{id}/alerts');
  }
}

const ALERT_TYPE_TR = { queue_length: 'Kuyruk uzunluğu', queue_wait: 'Bekleme süresi' };

function alertsTableHtml(alerts) {
  const deliveredCell = (v) => {
    if (v == null) return '<td class="num muted">—</td>';
    return v
      ? '<td class="num"><span class="deliv deliv--ok" title="Webhook teslim edildi">✓</span></td>'
      : '<td class="num"><span class="deliv deliv--fail" title="Webhook teslim edilemedi (delivered=false)">✗</span></td>';
  };
  const rows = alerts.map((a) => {
    const when = a.ts ? `${fmtDay(a.ts)} ${fmtClock(a.ts)}` : '—';
    const type = ALERT_TYPE_TR[a.type] ?? a.type ?? '—';
    const zone = a.zoneId != null ? zoneNameById(a.zoneId) : '—';
    return `<tr><td>${escapeHtml(when)}</td><td>${escapeHtml(type)}</td>` +
      `<td>${escapeHtml(zone)}</td>` +
      `<td class="num">${escapeHtml(a.len != null ? fmtInt(a.len) + ' kişi' : '—')}</td>` +
      `<td class="num">${escapeHtml(a.wait != null ? fmtDur(a.wait) : '—')}</td>` +
      deliveredCell(a.delivered) + '</tr>';
  }).join('');
  return '<div class="table-scroll"><table class="data-table">' +
    '<thead><tr><th scope="col">Zaman</th><th scope="col">Tip</th><th scope="col">Bölge</th>' +
    '<th scope="col">Kuyruk</th><th scope="col">Bekleme</th><th scope="col">Webhook</th></tr></thead>' +
    `<tbody>${rows}</tbody></table></div>`;
}

async function fillQueue(first) {
  const epoch = renderEpoch;
  const chartEl = $('#q-chart');
  if (!chartEl) return;
  queueInFlight += 1;
  try {
    const store = await ensureStore();
    if (isStale(epoch)) return; // görünüm değişti (C11)
    const qz = pickQueueZone(store);
    if (!qz) {
      showState(chartEl, 'Bu mağazada kuyruk tipi bölge tanımlı değil.');
      setKpi('kpi-qlen', '—', 'kuyruk bölgesi yok');
      setKpi('kpi-qwait', '—', '');
      setKpi('kpi-qcheck', '—', '');
      return;
    }
    const d = await apiGet(`/v1/stores/${state.storeId}/queues/${qz.id}/live`);
    // Geciken yanıt sıra bozamaz: görünüm bu arada yenilendiyse yazma (m5 + C11).
    if (isStale(epoch)) return;
    const q = normalizeQueueLive(d);

    setKpi('kpi-qlen',
      q.len != null ? `${fmtInt(q.len)} kişi` : '—',
      q.ts ? `son ölçüm ${fmtClock(q.ts)} · ${qz.name}` : qz.name);
    setKpi('kpi-qwait',
      q.wait != null ? fmtDur(q.wait) : '—',
      'alarm eşiği: 5 kişi veya 5 dk bekleme');
    setKpi('kpi-qcheck',
      q.active != null ? fmtInt(q.active) : '—',
      q.active != null ? 'açık kasa sayısı' : 'veri yok');

    $('#q-alert').innerHTML = q.alert
      ? '<div class="alert-band" role="alert">KASA AÇ — kuyruk alarm eşiği aşıldı (uzunluk ≥ 5 kişi veya bekleme ≥ 5 dk)</div>'
      : '';

    fillQueueChart(chartEl, q);
    $('#q-upd').textContent = `güncellendi ${fmtClock(new Date())} · 15 sn'de bir yenilenir`;
  } catch (err) {
    if (isStale(epoch)) return;
    if (first) {
      showError(chartEl, err);
      setKpiError('kpi-qlen', err);
      setKpiError('kpi-qwait', err);
      setKpiError('kpi-qcheck', err);
    } else {
      // Sessiz yenileme başarısız: eski görünümü koru, notu düşür.
      const upd = $('#q-upd');
      if (upd) upd.textContent = 'güncelleme başarısız — 15 sn sonra yeniden denenecek';
    }
  } finally {
    queueInFlight -= 1;
  }
}

function fillQueueChart(el, q) {
  if (!q.series.length) {
    showState(el, 'Henüz kuyruk ölçümü yok.');
    return;
  }
  const labels = q.series.map((p) => fmtClock(p.ts));
  const values = q.series.map((p) => p.len);
  const maxVal = Math.max(6, ...values);
  const series = [{
    name: 'Kuyruk uzunluğu',
    type: 'line',
    data: values,
    lineStyle: { color: COLORS.blue, width: 2 },
    itemStyle: { color: COLORS.blue, borderColor: COLORS.surface, borderWidth: 2 },
    symbol: 'circle',
    symbolSize: 7,
    areaStyle: { color: 'rgba(42,120,214,0.10)' },
    markLine: {
      silent: true,
      symbol: 'none',
      lineStyle: { color: COLORS.alertRed, type: 'dashed', width: 1 },
      label: {
        formatter: 'eşik: 5', color: COLORS.alertRed, fontSize: 11,
        position: 'insideEndTop',
      },
      data: [{ yAxis: 5 }],
    },
  }];
  if (q.alert) {
    // Alarm aktifken eşik üstü bölge kırmızı bantla vurgulanır.
    series[0].markArea = {
      silent: true,
      itemStyle: { color: 'rgba(220,38,38,0.08)' },
      data: [[{ yAxis: 5 }, { yAxis: maxVal + 1 }]],
    };
  }
  renderChart(el, baseOption({
    xAxis: catAxis(labels, { boundaryGap: false }),
    yAxis: valAxis({ name: 'kişi', minInterval: 1 }),
    series,
  }), {
    caption: 'Son 1 saat kuyruk ölçümleri',
    columns: ['Saat', 'Kuyruk uzunluğu', 'Tahmini bekleme'],
    rows: q.series.map((p) => [fmtClock(p.ts), fmtInt(p.len), fmtDur(p.wait)]),
  });
}

// ---------------------------------------------------------------------------
// 5) Brifing + Asistan
// ---------------------------------------------------------------------------

function briefingDate() {
  const now = new Date();
  if (state.range === 'yesterday') {
    return localDateStr(new Date(now.getTime() - 864e5));
  }
  return localDateStr(now);
}

async function viewBriefing(root) {
  root.innerHTML = `
    <div class="grid-2 grid-2--brief">
      <section class="panel">
        <div class="panel-head"><h2>Günlük brifing</h2><span class="panel-note" id="b-meta"></span></div>
        <div id="b-body" class="md-body"><div class="state-msg">Brifing hazırlanıyor…</div></div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>Asistana sor</h2><span class="panel-note">yanıtlar yalnız mağaza metriklerine dayanır</span></div>
        <form id="a-form" class="assist-form">
          <textarea id="a-q" rows="2" maxlength="500" required
            placeholder="Örn: Dün kasada ortalama bekleme ne kadardı?"></textarea>
          <button type="submit" class="btn btn--primary" id="a-send">Sor</button>
        </form>
        <div id="a-log" class="assist-log"></div>
      </section>
    </div>`;

  $('#a-form').addEventListener('submit', onAssistantAsk);
  fillBriefing();
}

async function fillBriefing() {
  const epoch = renderEpoch;
  const body = $('#b-body');
  const metaEl = $('#b-meta');
  try {
    const date = briefingDate();
    const d = await apiGet(`/v1/stores/${state.storeId}/briefing` + qs({ date }));
    if (isStale(epoch)) return; // görünüm değişti (C11)
    const md = d?.text_md ?? d?.markdown ?? d?.text ?? (typeof d === 'string' ? d : '');
    if (!md) {
      showState(body, 'Bu tarih için brifing bulunamadı.');
      return;
    }
    body.innerHTML = mdToHtml(md);
    metaEl.textContent = `${d?.date ?? date}${d?.provider ? ' · ' + d.provider : ''}`;
  } catch (err) {
    if (isStale(epoch)) return;
    showError(body, err);
  }
}

async function onAssistantAsk(e) {
  e.preventDefault();
  const input = $('#a-q');
  const question = input.value.trim();
  if (!question) return;
  const btn = $('#a-send');
  btn.disabled = true;
  btn.textContent = 'Yanıt hazırlanıyor…';

  const item = document.createElement('div');
  item.className = 'assist-item';
  item.innerHTML = '<div class="assist-q"></div>' +
    '<div class="assist-a"><div class="state-msg">Yanıt bekleniyor…</div></div>';
  item.querySelector('.assist-q').textContent = question;
  $('#a-log').prepend(item);

  try {
    const d = await apiPost(`/v1/stores/${state.storeId}/assistant`, { question });
    const answer = d?.answer_md ?? d?.answer ?? '';
    const used = Array.isArray(d?.metrics_used) ? d.metrics_used : [];
    item.querySelector('.assist-a').innerHTML =
      (answer ? mdToHtml(answer) : '<p class="muted">Yanıt boş döndü.</p>') +
      (used.length
        ? `<div class="chips">${used.map((u) =>
          `<span class="chip">${escapeHtml(typeof u === 'string' ? u : JSON.stringify(u))}</span>`).join('')}</div>`
        : '');
    input.value = '';
  } catch (err) {
    item.querySelector('.assist-a').innerHTML =
      `<div class="state-msg state-msg--err">${escapeHtml(friendly(err))}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sor';
  }
}

// ---------------------------------------------------------------------------
// 6) Yönetim (CONTRACTS §10) — mağaza ayarları, bölge editörü, cihazlar
// ---------------------------------------------------------------------------

async function viewAdmin(root) {
  const epoch = renderEpoch;
  root.innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>Mağaza ayarları</h2>
        <span class="panel-note">PUT /v1/stores/{id} — kısmi güncelleme</span>
      </div>
      <div id="ad-settings"><div class="state-msg">Yükleniyor…</div></div>
    </section>
    <section class="panel">
      <div class="panel-head">
        <h2>Bölge editörü</h2>
        <span class="panel-note">köşe sürükleme bu sürümde yok — şekli değiştirmek için silip yeniden çizin</span>
      </div>
      <div id="ad-zones"><div class="state-msg">Yükleniyor…</div></div>
      <div class="warn-note">Bölge değişiklikleri kenar cihaza <strong>otomatik yansımaz</strong>:
        kenar, bölge poligonlarını kendi YAML config'inden okur (CONTRACTS §10 notu).
        Üretim yolu imzalı config push'tur; şimdilik kenar config'ini elle güncelleyin.</div>
    </section>
    <section class="panel">
      <div class="panel-head">
        <h2>Kenar cihazları</h2>
        <span class="panel-note">GET /v1/stores/{id}/devices · heartbeat &lt; 2 dk = çevrimiçi</span>
      </div>
      <div id="ad-devices"><div class="state-msg">Yükleniyor…</div></div>
    </section>`;

  fillDevices(epoch);
  try {
    const store = await ensureStore();
    if (isStale(epoch)) return; // görünüm değişti (C11)
    renderStoreSettings($('#ad-settings'), store, epoch);
    renderZoneEditor($('#ad-zones'), store, epoch);
  } catch (err) {
    if (isStale(epoch)) return;
    showError($('#ad-settings'), err);
    showError($('#ad-zones'), err);
  }
}

// --- (a) mağaza ayarları formu ----------------------------------------------

function renderStoreSettings(el, store, epoch) {
  el.innerHTML = `
    <form id="st-form" class="form-grid">
      <div class="field">
        <label for="st-name">Mağaza adı</label>
        <input id="st-name" maxlength="120" required value="${escapeHtml(store.name)}">
      </div>
      <div class="field">
        <label for="st-w">Plan genişliği (m)</label>
        <input id="st-w" type="number" min="1" max="1000" step="0.1" required value="${escapeHtml(store.planW)}">
      </div>
      <div class="field">
        <label for="st-h">Plan yüksekliği (m)</label>
        <input id="st-h" type="number" min="1" max="1000" step="0.1" required value="${escapeHtml(store.planH)}">
      </div>
      <div class="field">
        <label for="st-tz">Zaman dilimi</label>
        <input id="st-tz" maxlength="60" placeholder="Europe/Istanbul" value="${escapeHtml(store.timezone)}">
      </div>
      <div class="field field--wide">
        <label for="st-wh">Webhook URL — kuyruk alarmları buraya POST edilir (§11); boş bırakılırsa kapalı</label>
        <input id="st-wh" type="url" maxlength="400" placeholder="https://ornek.tld/wherugo-alarm" value="${escapeHtml(store.webhookUrl)}">
      </div>
      <div class="form-actions field--wide">
        <button type="submit" class="btn btn--primary" id="st-save">Kaydet</button>
        <span class="form-msg" id="st-msg"></span>
      </div>
    </form>`;

  $('#st-form', el).addEventListener('submit', async (e) => {
    e.preventDefault();
    const msg = $('#st-msg', el);
    const name = $('#st-name', el).value.trim();
    const w = Number($('#st-w', el).value);
    const h = Number($('#st-h', el).value);
    const tz = $('#st-tz', el).value.trim();
    const webhook = $('#st-wh', el).value.trim();
    if (!name) { setFormMsg(msg, 'Mağaza adı boş olamaz.', 'err'); return; }
    if (!(w > 0) || !(h > 0)) { setFormMsg(msg, 'Plan boyutları pozitif olmalı.', 'err'); return; }
    // Kısmi güncelleme (§10): webhook_url boşsa null gönderilir (alanı temizler).
    const body = { name, plan_width_m: w, plan_height_m: h, webhook_url: webhook || null };
    if (tz) body.timezone = tz;
    const btn = $('#st-save', el);
    btn.disabled = true;
    setFormMsg(msg, 'Kaydediliyor…');
    try {
      await apiPut(`/v1/stores/${state.storeId}`, body);
      await refreshStore(); // üst bar adı + diğer sekmeler taze veriyi görsün
      if (isStale(epoch) || !el.isConnected) return;
      setFormMsg(msg, 'Kaydedildi.', 'ok');
    } catch (err) {
      if (isStale(epoch) || !el.isConnected) return;
      setFormMsg(msg, isV2Missing(err)
        ? 'Backend mağaza güncellemeyi henüz desteklemiyor (PUT /v1/stores/{id} — v2 bekleniyor).'
        : friendly(err), 'err');
    } finally {
      if (el.isConnected) btn.disabled = false;
    }
  });
}

// --- (b) bölge editörü ---------------------------------------------------------
// Plan canvas'ı ısı haritasıyla aynı görsel dili kullanır. "Yeni bölge" modunda
// tıklamalar poligon köşesi ekler (metre, 2 ondalık); çift tık veya "Bitir"
// poligonu kapatıp POST /v1/stores/{id}/zones çağırır. Köşe sürükleyerek
// düzenleme v1'de bilinçli olarak yok (CONTRACTS notu): sil + yeniden çiz.

function renderZoneEditor(el, store, epoch, notice = '') {
  const draft = { drawing: false, pts: [] }; // pts: [[x_m, y_m], ...]
  const typeOptions = Object.entries(ZONE_TYPE_TR).map(([v, l]) =>
    `<option value="${escapeHtml(v)}">${escapeHtml(l)}</option>`).join('');

  el.innerHTML = `
    <div class="zed-layout">
      <div>
        <div class="zed-toolbar">
          <button type="button" class="btn" id="zed-new">Yeni bölge</button>
          <span class="zed-hint" id="zed-hint">Yeni bölge çizmek için "Yeni bölge"ye tıklayın.</span>
        </div>
        <div class="zed-canvas-wrap"><canvas id="zed-canvas" class="zed-canvas"></canvas></div>
        <div id="zed-draft" class="zed-draft" hidden>
          <h3>Yeni bölge</h3>
          <div class="form-grid">
            <div class="field">
              <label for="zed-name">Ad</label>
              <input id="zed-name" maxlength="80" placeholder="örn. Aksesuar Reyonu">
            </div>
            <div class="field">
              <label for="zed-type">Tip</label>
              <select id="zed-type">${typeOptions}</select>
            </div>
            <div class="field">
              <label for="zed-cat">Kategori (isteğe bağlı)</label>
              <input id="zed-cat" maxlength="80" placeholder="örn. kadın giyim">
            </div>
          </div>
          <div class="form-actions">
            <button type="button" class="btn btn--primary" id="zed-finish" disabled>Bitir (0 köşe)</button>
            <button type="button" class="btn" id="zed-cancel">İptal</button>
            <span class="form-msg" id="zed-msg"></span>
          </div>
        </div>
      </div>
      <div>
        <div class="form-msg" id="zlist-msg"></div>
        <div class="zone-list" id="zed-list">${
          store.zones.length
            ? store.zones.map((z) => zoneRowHtml(z)).join('')
            : '<div class="state-msg">Tanımlı bölge yok — soldaki plandan ilk bölgeyi çizin.</div>'
        }</div>
      </div>
    </div>`;

  const listMsg = $('#zlist-msg', el);
  if (notice) setFormMsg(listMsg, notice, 'ok');

  const canvas = $('#zed-canvas', el);
  let scale = drawEditorCanvas(canvas, store, draft);

  const round2 = (v) => Math.round(v * 100) / 100;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  function updateDraftUi() {
    const finish = $('#zed-finish', el);
    finish.disabled = draft.pts.length < 3;
    finish.textContent = `Bitir (${draft.pts.length} köşe)`;
    $('#zed-hint', el).textContent = draft.drawing
      ? 'Köşe eklemek için plana tıklayın (en az 3). Çift tık veya "Bitir" poligonu kapatır.'
      : 'Yeni bölge çizmek için "Yeni bölge"ye tıklayın.';
  }

  canvas.addEventListener('click', (e) => {
    if (!draft.drawing) return;
    const rect = canvas.getBoundingClientRect();
    const x = round2(clamp((e.clientX - rect.left) / scale, 0, store.planW));
    const y = round2(clamp((e.clientY - rect.top) / scale, 0, store.planH));
    const last = draft.pts[draft.pts.length - 1];
    // Çift tık iki click üretir: son köşenin dibindeki tekrarı yut.
    if (last && Math.hypot(last[0] - x, last[1] - y) < 0.15) return;
    draft.pts.push([x, y]);
    scale = drawEditorCanvas(canvas, store, draft);
    updateDraftUi();
  });

  canvas.addEventListener('dblclick', (e) => {
    e.preventDefault();
    if (draft.drawing && draft.pts.length >= 3) submitNewZone();
  });

  $('#zed-new', el).addEventListener('click', () => {
    draft.drawing = true;
    draft.pts = [];
    $('#zed-draft', el).hidden = false;
    $('#zed-new', el).disabled = true;
    canvas.classList.add('is-drawing');
    setFormMsg($('#zed-msg', el), '');
    scale = drawEditorCanvas(canvas, store, draft);
    updateDraftUi();
  });

  $('#zed-cancel', el).addEventListener('click', () => {
    draft.drawing = false;
    draft.pts = [];
    $('#zed-draft', el).hidden = true;
    $('#zed-new', el).disabled = false;
    canvas.classList.remove('is-drawing');
    scale = drawEditorCanvas(canvas, store, draft);
    updateDraftUi();
  });

  $('#zed-finish', el).addEventListener('click', submitNewZone);

  async function submitNewZone() {
    const msg = $('#zed-msg', el);
    const name = $('#zed-name', el).value.trim();
    const zoneType = $('#zed-type', el).value;
    const category = $('#zed-cat', el).value.trim();
    if (draft.pts.length < 3) { setFormMsg(msg, 'En az 3 köşe gerekli.', 'err'); return; }
    if (!name) {
      setFormMsg(msg, 'Bölge adı gerekli.', 'err');
      $('#zed-name', el).focus();
      return;
    }
    // CONTRACTS §10: {name, zone_type, polygon, category?} — polygon plan
    // koordinatında (metre) [[x,y], ...] köşe listesi olarak gönderilir.
    const body = { name, zone_type: zoneType, polygon: draft.pts.map((p) => [p[0], p[1]]) };
    if (category) body.category = category;
    const finish = $('#zed-finish', el);
    finish.disabled = true;
    setFormMsg(msg, 'Kaydediliyor…');
    try {
      await apiPost(`/v1/stores/${state.storeId}/zones`, body);
      const fresh = await refreshStore(); // ısı haritası dahil herkes yeni bölgeyi görür
      if (isStale(epoch) || !el.isConnected) return;
      renderZoneEditor(el, fresh, epoch, `"${name}" bölgesi eklendi.`);
    } catch (err) {
      if (isStale(epoch) || !el.isConnected) return;
      finish.disabled = draft.pts.length < 3;
      setFormMsg(msg, isV2Missing(err)
        ? 'Backend bölge eklemeyi henüz desteklemiyor (POST /v1/stores/{id}/zones — v2 bekleniyor).'
        : friendly(err), 'err');
    }
  }

  // Bölge listesi: kaydet (PUT) / sil (DELETE) — tek delegasyon.
  $('#zed-list', el).addEventListener('click', async (e) => {
    const row = e.target.closest('.zone-row');
    if (!row) return;
    const zid = row.dataset.zid;
    const zone = store.zones.find((z) => String(z.id) === String(zid));
    if (!zone) return;
    const buttons = $$('button', row);

    if (e.target.closest('[data-zsave]')) {
      const name = row.querySelector('[data-f="name"]').value.trim();
      const zoneType = row.querySelector('[data-f="type"]').value;
      const category = row.querySelector('[data-f="cat"]').value.trim();
      if (!name) { setFormMsg(listMsg, 'Bölge adı boş olamaz.', 'err'); return; }
      buttons.forEach((b) => { b.disabled = true; });
      setFormMsg(listMsg, `"${name}" kaydediliyor…`);
      try {
        // Poligon burada değiştirilemez (köşe sürükleme v1'de yok) — yalnız
        // ad/tip/kategori güncellenir; sunucu poligonu korur (kısmi güncelleme).
        await apiPut(`/v1/stores/${state.storeId}/zones/${zid}`,
          { name, zone_type: zoneType, category: category || null });
        const fresh = await refreshStore();
        if (isStale(epoch) || !el.isConnected) return;
        renderZoneEditor(el, fresh, epoch, `"${name}" güncellendi.`);
      } catch (err) {
        if (isStale(epoch) || !el.isConnected) return;
        buttons.forEach((b) => { b.disabled = false; });
        setFormMsg(listMsg, isV2Missing(err)
          ? 'Backend bölge güncellemeyi henüz desteklemiyor (PUT /v1/stores/{id}/zones/{zid} — v2 bekleniyor).'
          : friendly(err), 'err');
      }
      return;
    }

    if (e.target.closest('[data-zdel]')) {
      if (!window.confirm(`"${zone.name}" bölgesi silinsin mi?\nGeçmiş ziyaret verisi korunur (CONTRACTS §10).`)) return;
      buttons.forEach((b) => { b.disabled = true; });
      setFormMsg(listMsg, `"${zone.name}" siliniyor…`);
      try {
        await apiDelete(`/v1/stores/${state.storeId}/zones/${zid}`);
        const fresh = await refreshStore();
        if (isStale(epoch) || !el.isConnected) return;
        renderZoneEditor(el, fresh, epoch, `"${zone.name}" silindi.`);
      } catch (err) {
        if (isStale(epoch) || !el.isConnected) return;
        buttons.forEach((b) => { b.disabled = false; });
        setFormMsg(listMsg, isV2Missing(err)
          ? 'Backend bölge silmeyi henüz desteklemiyor (DELETE /v1/stores/{id}/zones/{zid} — v2 bekleniyor).'
          : friendly(err), 'err');
      }
    }
  });
}

function zoneRowHtml(z) {
  const known = Object.prototype.hasOwnProperty.call(ZONE_TYPE_TR, z.type);
  const options = Object.entries(ZONE_TYPE_TR).map(([v, l]) =>
    `<option value="${escapeHtml(v)}"${v === z.type ? ' selected' : ''}>${escapeHtml(l)}</option>`).join('') +
    (known ? '' : `<option value="${escapeHtml(z.type)}" selected>${escapeHtml(z.type)}</option>`);
  return `<div class="zone-row" data-zid="${escapeHtml(z.id)}">
    <input data-f="name" maxlength="80" aria-label="Bölge adı" value="${escapeHtml(z.name)}">
    <select data-f="type" aria-label="Bölge tipi">${options}</select>
    <input data-f="cat" maxlength="80" placeholder="kategori" aria-label="Kategori" value="${escapeHtml(z.category)}">
    <button type="button" class="btn btn--sm" data-zsave>Kaydet</button>
    <button type="button" class="btn btn--sm btn--danger" data-zdel>Sil</button>
    ${z.poly ? '' : '<div class="zone-nopoly">Poligon tanımsız — planda ve ısı haritasında çizilemez.</div>'}
  </div>`;
}

/** Plan + mevcut bölgeler + çizilmekte olan taslak poligon; px/metre ölçeğini döner. */
function drawEditorCanvas(canvas, store, draft) {
  const wrap = canvas.parentElement;
  const cssW = Math.max(300, Math.min(wrap.clientWidth || 640, 760));
  const scale = cssW / store.planW;
  const cssH = Math.round(store.planH * scale);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  canvas.style.width = cssW + 'px';
  canvas.style.height = cssH + 'px';

  const ctx = canvas.getContext('2d');
  if (!ctx) return scale;
  ctx.scale(dpr, dpr);

  // Zemin + metre gridi (ısı haritasıyla aynı görsel dil)
  ctx.fillStyle = '#f6f5f1';
  ctx.fillRect(0, 0, cssW, cssH);
  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 1;
  for (let gx = 5; gx < store.planW; gx += 5) {
    ctx.beginPath(); ctx.moveTo(gx * scale + 0.5, 0); ctx.lineTo(gx * scale + 0.5, cssH); ctx.stroke();
  }
  for (let gy = 5; gy < store.planH; gy += 5) {
    ctx.beginPath(); ctx.moveTo(0, gy * scale + 0.5); ctx.lineTo(cssW, gy * scale + 0.5); ctx.stroke();
  }

  // Mevcut bölgeler
  ctx.font = `11px ${'system-ui, sans-serif'}`;
  for (const z of store.zones) {
    if (!z.poly) continue;
    ctx.beginPath();
    z.poly.forEach(([px, py], i) => {
      if (i) ctx.lineTo(px * scale, py * scale); else ctx.moveTo(px * scale, py * scale);
    });
    ctx.closePath();
    ctx.fillStyle = 'rgba(42,120,214,0.06)';
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = 'rgba(11,11,11,0.45)';
    ctx.stroke();

    const cx = z.poly.reduce((a, p) => a + p[0], 0) / z.poly.length * scale;
    const cy = z.poly.reduce((a, p) => a + p[1], 0) / z.poly.length * scale;
    const w = ctx.measureText(z.name).width;
    ctx.lineWidth = 3;
    ctx.strokeStyle = 'rgba(252,252,251,0.9)';
    ctx.strokeText(z.name, cx - w / 2, cy + 4);
    ctx.fillStyle = COLORS.inkSecondary;
    ctx.fillText(z.name, cx - w / 2, cy + 4);
  }

  // Taslak poligon (çizim modunda): mavi kesikli kontur + köşe noktaları
  if (draft && draft.pts.length) {
    ctx.beginPath();
    draft.pts.forEach(([px, py], i) => {
      if (i) ctx.lineTo(px * scale, py * scale); else ctx.moveTo(px * scale, py * scale);
    });
    if (draft.pts.length >= 3) ctx.closePath();
    ctx.setLineDash([6, 4]);
    ctx.lineWidth = 2;
    ctx.strokeStyle = COLORS.blue;
    ctx.stroke();
    ctx.setLineDash([]);
    if (draft.pts.length >= 3) {
      ctx.fillStyle = 'rgba(42,120,214,0.12)';
      ctx.fill();
    }
    draft.pts.forEach(([px, py], i) => {
      ctx.beginPath();
      ctx.arc(px * scale, py * scale, i === 0 ? 5 : 4, 0, Math.PI * 2);
      ctx.fillStyle = i === 0 ? COLORS.surface : COLORS.blue;
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = COLORS.blue;
      ctx.stroke();
    });
  }

  // Çerçeve
  ctx.strokeStyle = COLORS.axis;
  ctx.lineWidth = 1;
  ctx.strokeRect(0.5, 0.5, cssW - 1, cssH - 1);
  return scale;
}

// --- (c) cihaz listesi -----------------------------------------------------------

function normalizeDevices(d) {
  const list = Array.isArray(d) ? d : (d?.devices ?? d?.items ?? []);
  if (!Array.isArray(list)) return [];
  return list.map((x) => ({
    id: x?.id ?? x?.device_id ?? '—',
    name: x?.name ?? String(x?.id ?? '—'),
    hb: parseTs(x?.last_heartbeat ?? x?.heartbeat ?? x?.last_seen),
    online: typeof x?.online === 'boolean' ? x.online : null,
  }));
}

function deviceBadgeHtml(dev) {
  const online = dev.online != null
    ? dev.online
    : (dev.hb ? (Date.now() - dev.hb.getTime()) < DEVICE_ONLINE_MS : null);
  if (online === true) {
    return '<span class="dev-badge dev-badge--on"><span class="q-dot" aria-hidden="true"></span>Çevrimiçi</span>';
  }
  if (online === false) {
    return '<span class="dev-badge dev-badge--off"><span class="q-dot" aria-hidden="true"></span>Çevrimdışı</span>';
  }
  return '<span class="dev-badge dev-badge--unknown"><span class="q-dot" aria-hidden="true"></span>Bilinmiyor</span>';
}

async function fillDevices(epoch) {
  const el = $('#ad-devices');
  if (!el) return;
  try {
    const d = await apiGet(`/v1/stores/${state.storeId}/devices`);
    if (isStale(epoch)) return; // görünüm değişti (C11)
    const devices = normalizeDevices(d);
    if (!devices.length) {
      showState(el, 'Kayıtlı kenar cihazı yok.');
      return;
    }
    const rows = devices.map((dev) => {
      const hb = dev.hb ? `${fmtDay(dev.hb)} ${fmtClock(dev.hb)}` : '—';
      return `<tr><td>${escapeHtml(dev.id)}</td><td>${escapeHtml(dev.name)}</td>` +
        `<td class="num">${escapeHtml(hb)}</td><td>${deviceBadgeHtml(dev)}</td></tr>`;
    }).join('');
    el.innerHTML = '<div class="table-scroll"><table class="data-table">' +
      '<thead><tr><th scope="col">Cihaz ID</th><th scope="col">Ad</th>' +
      '<th scope="col">Son heartbeat</th><th scope="col">Durum</th></tr></thead>' +
      `<tbody>${rows}</tbody></table></div>`;
  } catch (err) {
    if (isStale(epoch)) return;
    showV2State(el, err, 'GET /v1/stores/{id}/devices');
  }
}

// ---------------------------------------------------------------------------
// Başlangıç
// ---------------------------------------------------------------------------

function init() {
  wireTopbar();
  wireAuth();
  // Backend kapalı olsa bile sayfa kurulur; paneller kendi durumunu gösterir.
  bootstrap(false).finally(() => renderCurrent());
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
