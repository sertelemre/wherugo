// WherUGo dashboard — API erişim katmanı.
// Tüm istekler CONTRACTS §4'teki sözleşmeye göre, göreceli /v1 yollarına ve
// `Authorization: Bearer demo` başlığıyla gider (backend dashboard'u / kökünden servis eder).

export class ApiError extends Error {
  constructor(message, { status = 0, offline = false } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.offline = offline;
  }
}

async function request(path, options = {}) {
  let res;
  try {
    res = await fetch(path, {
      ...options,
      headers: {
        Authorization: 'Bearer demo',
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
    throw new ApiError(`Sunucu hatası (HTTP ${res.status})${detail ? ': ' + detail : ''}`, { status: res.status });
  }
  try {
    return await res.json();
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

/** Sorgu dizesi kurar; null/undefined parametreleri atlar. */
export function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== '') u.set(k, v);
  }
  const s = u.toString();
  return s ? `?${s}` : '';
}
