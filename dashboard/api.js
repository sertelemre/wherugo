// WherUGo dashboard — API erişim katmanı.
// Tüm istekler CONTRACTS §4'teki sözleşmeye göre göreceli /v1 yollarına gider
// (backend dashboard'u / kökünden servis eder). Auth v2 (CONTRACTS §9):
// token localStorage'da saklanır; hiç token kaydedilmemişse 'demo' varsayılır
// (demo modda backend `Bearer demo` kabul eder). Sunucu 401 dönerse kayıtlı
// onUnauthorized işleyicisi tetiklenir — app.js token giriş panelini açar.

const TOKEN_KEY = 'wherugo_token';

export class ApiError extends Error {
  constructor(message, { status = 0, offline = false } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.offline = offline;
  }
}

/** Kayıtlı token; yoksa 'demo' (CONTRACTS §9 — demo modu varsayılan). */
export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || 'demo'; } catch { return 'demo'; }
}

export function setToken(token) {
  try { localStorage.setItem(TOKEN_KEY, token); } catch { /* private mod vb. */ }
}

export function clearToken() {
  try { localStorage.removeItem(TOKEN_KEY); } catch { /* yoksay */ }
}

/** Kullanıcı gerçekten token kaydetmiş mi (varsayılan demo değil)? */
export function hasStoredToken() {
  try { return Boolean(localStorage.getItem(TOKEN_KEY)); } catch { return false; }
}

// 401 işleyicisi — app.js kaydeder (token giriş panelini açar).
let unauthorizedHandler = null;

export function onUnauthorized(handler) {
  unauthorizedHandler = handler;
}

/**
 * Ortak istek çekirdeği.
 * flags.noAuth   — Authorization başlığı eklenmez (ör. /v1/auth/token).
 * flags.no401    — 401'de panel tetiklenmez (panelin kendi isteği için).
 */
async function request(path, options = {}, flags = {}) {
  let res;
  try {
    res = await fetch(path, {
      ...options,
      headers: {
        ...(flags.noAuth ? {} : { Authorization: `Bearer ${getToken()}` }),
        ...(options.headers || {}),
      },
    });
  } catch {
    // Ağ hatası: backend henüz ayakta değil, ya da bağlantı koptu.
    throw new ApiError("Backend'e ulaşılamıyor — sunucu henüz çalışmıyor olabilir.", { offline: true });
  }
  if (!res.ok) {
    let detail = '';
    try {
      const j = await res.json();
      detail = typeof j.detail === 'string' ? j.detail : (j.message || '');
    } catch { /* gövde JSON değilse ayrıntısız devam */ }
    if (res.status === 401 && !flags.no401 && unauthorizedHandler) {
      try { unauthorizedHandler(detail); } catch { /* panel hatası isteği bozmasın */ }
    }
    throw new ApiError(`Sunucu hatası (HTTP ${res.status})${detail ? ': ' + detail : ''}`, { status: res.status });
  }
  // 204 / boş gövde (ör. DELETE) geçerli başarıdır.
  if (res.status === 204) return null;
  const text = await res.text();
  if (!text.trim()) return null;
  try {
    return JSON.parse(text);
  } catch {
    throw new ApiError('Sunucu yanıtı çözümlenemedi (geçersiz JSON).');
  }
}

export function apiGet(path) {
  return request(path);
}

export function apiPost(path, body) {
  return request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function apiPut(path, body) {
  return request(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function apiDelete(path) {
  return request(path, { method: 'DELETE' });
}

/**
 * CONTRACTS §9: POST /v1/auth/token {api_key} → {token, expires_in}.
 * Auth başlığı gönderilmez; 401 (yanlış anahtar) paneli yeniden tetiklemez —
 * hata, panelin kendi form akışında gösterilir.
 */
export async function requestToken(apiKey) {
  const d = await request('/v1/auth/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_key: apiKey }),
  }, { noAuth: true, no401: true });
  if (!d || typeof d.token !== 'string' || !d.token) {
    throw new ApiError('Sunucu geçerli bir token döndürmedi.');
  }
  return d; // {token, expires_in}
}

/** Sorgu dizesi kurar; null/undefined parametreleri atlar. */
export function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== '') u.set(k, v);
  }
  const s = u.toString();
  return s ? `?${s}` : '';
}
