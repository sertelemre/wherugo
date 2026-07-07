// WherUGo dashboard — grafik katmanı.
// ECharts CDN'den yüklenir; yüklenemez ya da çizim hata verirse her grafik,
// aynı veriyi gösteren HTML tablo fallback'ine düşer (CONTRACTS §7).

import { escapeHtml } from './format.js';

export const FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif';

// Renk sistemi (dataviz referans paleti — doğrulanmış sıralama, asla döngülenmez).
export const COLORS = {
  blue: '#2a78d6',      // seri 1 / birincil ölçü
  blueLight: '#86b6ef', // aynı ölçünün açık tonu (p95 gibi ikincil istatistik)
  aqua: '#1baf7a',
  ink: '#0b0b0b',
  inkSecondary: '#52514e',
  muted: '#898781',
  grid: '#e1e0d9',
  axis: '#c3c2b7',
  surface: '#fcfcfb',
  other: '#898781',     // "Diğer" dilimi — kuyruk rengi değil, nötr gri
  alertRed: '#dc2626',  // kuyruk alarm bandı (spesifikasyon rengi)
};

// Kategorik palet — sabit sırayla atanır (pasta grafiği vb.).
export const SERIES = ['#2a78d6', '#1baf7a', '#eda100', '#008300', '#4a3aa7', '#e34948', '#e87ba4', '#eb6834'];

// Tek-ton mavi sequential rampa (ısı haritası: açık = az, koyu = çok).
export const SEQ_BLUE = [
  '#cde2fb', '#b7d3f6', '#9ec5f4', '#86b6ef', '#6da7ec', '#5598e7',
  '#3987e5', '#2a78d6', '#256abf', '#1c5cab', '#184f95', '#104281', '#0d366b',
];

// Kalite rozeti renkleri — CONTRACTS §7 / görev spesifikasyonu ile sabit.
export const QUALITY = { green: '#16a34a', yellow: '#d97706', red: '#dc2626' };

export function echartsReady() {
  return typeof window !== 'undefined'
    && typeof window.echarts !== 'undefined'
    && typeof window.echarts.init === 'function';
}

// Görünüm değişince DOM'dan kopan instance'ları temizlemek için kayıt tutulur.
const registry = new Map();

function pruneCharts() {
  for (const [el, inst] of registry) {
    if (!el.isConnected) {
      try { inst.dispose(); } catch { /* zaten dispose edilmiş olabilir */ }
      registry.delete(el);
    }
  }
}

if (typeof window !== 'undefined') {
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      pruneCharts();
      for (const inst of registry.values()) {
        try { inst.resize(); } catch { /* yoksay */ }
      }
    }, 150);
  });
}

/**
 * Grafiği çizer; ECharts yoksa/başarısızsa fallback tabloyu basar.
 * @param {HTMLElement} el grafik kabı
 * @param {object} option ECharts option
 * @param {object|null} fallback dataTable() spesifikasyonu — {caption, columns, rows, numFrom}
 */
export function renderChart(el, option, fallback) {
  pruneCharts();
  if (echartsReady()) {
    try {
      const prev = window.echarts.getInstanceByDom(el);
      if (prev) prev.dispose();
      el.innerHTML = '';
      const inst = window.echarts.init(el);
      inst.setOption(option);
      registry.set(el, inst);
      return inst;
    } catch (err) {
      console.warn('[wherugo] ECharts çizimi başarısız, tablo görünümüne düşülüyor:', err);
    }
  }
  el.classList.add('chart--fallback');
  el.innerHTML = fallback
    ? dataTable(fallback)
    : '<div class="state-msg">Grafik kütüphanesi yüklenemedi; veri tablo olarak da hazırlanamadı.</div>';
  return null;
}

/**
 * Erişilebilir HTML veri tablosu üretir (grafik fallback'i ve matrisler için).
 * rows hücreleri önceden biçimlenmiş string'lerdir; hepsi HTML-kaçırılır.
 * numFrom: bu indeksten itibaren sütunlar sağa yaslanır (sayısal).
 */
export function dataTable({ caption, columns, rows, numFrom = 1, note }) {
  const head = columns.map((c) => `<th scope="col">${escapeHtml(c)}</th>`).join('');
  const body = rows.length
    ? rows.map((r) =>
        `<tr>${r.map((c, i) =>
          `<td${i >= numFrom ? ' class="num"' : ''}>${escapeHtml(c ?? '—')}</td>`).join('')}</tr>`
      ).join('')
    : `<tr><td colspan="${columns.length}" class="muted">Veri yok</td></tr>`;
  return `<div class="table-scroll"><table class="data-table">${
    caption ? `<caption>${escapeHtml(caption)}</caption>` : ''
  }<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>${
    note ? `<p class="table-note">${escapeHtml(note)}</p>` : ''
  }`;
}

/** Ortak ECharts görünüm dili: sistem fontu, ince eksenler, sessiz grid. */
export function baseOption(extra = {}) {
  return {
    animationDuration: 250,
    textStyle: { fontFamily: FONT },
    tooltip: axisTooltip(),
    grid: { left: 12, right: 18, top: 38, bottom: 10, containLabel: true },
    ...extra,
  };
}

export function axisTooltip(extra = {}) {
  return {
    trigger: 'axis',
    axisPointer: { type: 'line', lineStyle: { color: COLORS.axis } },
    backgroundColor: '#ffffff',
    borderColor: 'rgba(11,11,11,0.12)',
    borderWidth: 1,
    padding: [8, 10],
    textStyle: { color: COLORS.ink, fontSize: 12, fontFamily: FONT },
    extraCssText: 'box-shadow:0 4px 16px rgba(11,11,11,0.08);border-radius:8px;',
    ...extra,
  };
}

export function itemTooltip(extra = {}) {
  return axisTooltip({ trigger: 'item', axisPointer: undefined, ...extra });
}

export function catAxis(labels, extra = {}) {
  return {
    type: 'category',
    data: labels,
    axisLine: { lineStyle: { color: COLORS.axis } },
    axisTick: { show: false },
    axisLabel: { color: COLORS.muted, fontSize: 11 },
    ...extra,
  };
}

export function valAxis(extra = {}) {
  return {
    type: 'value',
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: COLORS.muted, fontSize: 11 },
    splitLine: { lineStyle: { color: COLORS.grid, width: 1 } },
    nameTextStyle: { color: COLORS.muted, fontSize: 11 },
    ...extra,
  };
}

export function legendStyle(extra = {}) {
  return {
    top: 0,
    left: 0,
    itemWidth: 12,
    itemHeight: 8,
    textStyle: { color: COLORS.inkSecondary, fontSize: 11, fontFamily: FONT },
    ...extra,
  };
}

/** 0..1 normalleştirilmiş değeri sequential mavi rampaya eşler. */
export function rampColor(t) {
  const i = Math.round(Math.max(0, Math.min(1, t)) * (SEQ_BLUE.length - 1));
  return SEQ_BLUE[i];
}
