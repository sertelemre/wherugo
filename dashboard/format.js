// WherUGo dashboard — metin, sayı ve tarih biçimleme yardımcıları (bağımlılıksız).

const NF_INT = new Intl.NumberFormat('tr-TR', { maximumFractionDigits: 0 });
const NF_1 = new Intl.NumberFormat('tr-TR', { maximumFractionDigits: 1 });

/** HTML meta karakterlerini kaçırır — API'den gelen her metin buradan geçer. */
export function escapeHtml(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

export function fmtInt(v) {
  const n = Number(v);
  return v == null || !Number.isFinite(n) ? '—' : NF_INT.format(n);
}

export function fmtNum1(v) {
  const n = Number(v);
  return v == null || !Number.isFinite(n) ? '—' : NF_1.format(n);
}

/** Oran (0-1) veya yüzde (0-100) kabul eder, "%23,4" biçiminde döner. */
export function fmtPct(v) {
  let n = Number(v);
  if (v == null || !Number.isFinite(n)) return '—';
  if (n <= 1) n *= 100;
  return '%' + NF_1.format(n);
}

/** Saniyeyi "42 sn" / "3 dk 10 sn" biçimine çevirir. */
export function fmtDur(sec) {
  const n = Number(sec);
  if (sec == null || !Number.isFinite(n)) return '—';
  const s = Math.round(n);
  if (s < 60) return `${s} sn`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  return r ? `${m} dk ${r} sn` : `${m} dk`;
}

/** Zaman damgasını Date'e çevirir: RFC3339 string, epoch sn veya epoch ms kabul eder. */
export function parseTs(v) {
  if (v == null) return null;
  if (v instanceof Date) return Number.isNaN(v.getTime()) ? null : v;
  if (typeof v === 'number' || /^\d+(\.\d+)?$/.test(String(v).trim())) {
    const n = Number(v);
    return new Date(n > 1e12 ? n : n * 1000);
  }
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function fmtHour(d) {
  return d ? String(d.getHours()).padStart(2, '0') + ':00' : '—';
}

export function fmtClock(d) {
  return d ? d.toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' }) : '—';
}

export function fmtDay(d) {
  return d ? d.toLocaleDateString('tr-TR', { day: 'numeric', month: 'short' }) : '—';
}

/** Yerel takvim günü — API'nin `date` parametresi için (YYYY-MM-DD). */
export function localDateStr(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/**
 * Çok küçük Markdown → HTML dönüştürücü (kütüphanesiz; CONTRACTS §7).
 * Desteklenen: #..#### başlık, - / * / 1. listeler, **kalın**, *italik*,
 * `kod`, > alıntı, paragraf. Girdi önce HTML-kaçırılır — XSS güvenli.
 */
export function mdToHtml(md) {
  const lines = String(md ?? '').replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let list = null; // 'ul' | 'ol' | null
  let para = [];

  const flushPara = () => {
    if (para.length) { out.push(`<p>${inlineMd(para.join(' '))}</p>`); para = []; }
  };
  const closeList = () => {
    if (list) { out.push(`</${list}>`); list = null; }
  };

  for (const raw of lines) {
    const t = raw.trim();
    if (!t) { flushPara(); closeList(); continue; }

    const h = t.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flushPara(); closeList();
      const lvl = Math.min(h[1].length + 2, 6); // # → h3 (panel başlığı h2'nin altına oturur)
      out.push(`<h${lvl}>${inlineMd(h[2])}</h${lvl}>`);
      continue;
    }
    const ul = t.match(/^[-*•]\s+(.*)$/);
    if (ul) {
      flushPara();
      if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; }
      out.push(`<li>${inlineMd(ul[1])}</li>`);
      continue;
    }
    const ol = t.match(/^\d+[.)]\s+(.*)$/);
    if (ol) {
      flushPara();
      if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; }
      out.push(`<li>${inlineMd(ol[1])}</li>`);
      continue;
    }
    if (t.startsWith('>')) {
      flushPara(); closeList();
      out.push(`<blockquote><p>${inlineMd(t.replace(/^>\s?/, ''))}</p></blockquote>`);
      continue;
    }
    para.push(t);
  }
  flushPara(); closeList();
  return out.join('\n');
}

function inlineMd(text) {
  let s = escapeHtml(text);
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*(?!\*)/g, '$1<em>$2</em>');
  return s;
}
