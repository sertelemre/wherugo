# WherUGo — İmplementasyon Sözleşmesi (v1)

Bu doküman, monorepo bileşenlerinin **birbirine karşı programlandığı** bağlayıcı sözleşmedir.
Mimari gerekçeler için `docs/02-sistem-mimarisi.md`; bu dosya yalnız "ne implemente edilir"i tanımlar.

## 0. MVP Sadeleştirmeleri (bilinçli sapmalar)

Üretim planı (docs/02) ClickHouse + EMQX/MQTT + Protobuf der. Bu repo'nun **çalışan MVP demo**su,
aynı sözleşme alanlarını koruyarak şu ikamelerle başlar (geçiş yolları docs/02'de):

| Üretim (plan) | MVP (bu repo) | Korunan sözleşme |
|---|---|---|
| Protobuf zarfı | JSON zarfı (aynı alanlar) | `schema_version: 3` dahil tüm zarf alanları |
| MQTT (Mosquitto→EMQX) | HTTP batch `POST /v1/ingest/events` + SQLite spool (store-and-forward) | at-least-once + `event_id` idempotensi + `seq_no` boşluk tespiti |
| ClickHouse + PG | SQLAlchemy (varsayılan SQLite; `DATABASE_URL` ile PostgreSQL) | tablo/alan adları |
| D-FINE/ByteTrack (Jetson) | `--source simulate` sentetik üreteç; `[cv]` extra ile ultralytics+OpenCV opsiyonel | olay şeması aynı — analitik katman kaynağı ayırt edemez |

**Gizlilik değişmezleri MVP'de de mutlak:** hiçbir piksel/frame publisher'a girmez (olay tiplerinde
görüntü alanı yoktur); `track_id` oturum-yerel; yüz tanıma/demografi kodu yazılmaz; agregat uçlarda k<10 bastırma.

## 1. Monorepo Yerleşimi

```
edge/       → wherugo_edge   (Python paketi: kaynak→takip→homografi→zone motoru→publisher)
backend/    → wherugo_backend (FastAPI: ingest + analitik + API + dashboard servisi)
ai/         → wherugo_ai     (sağlayıcı-bağımsız LLM/VLM katmanı: brifing + asistan; backend'den BAĞIMSIZ)
dashboard/  → statik SPA     (backend / kökünden servis edilir)
deploy/     → docker-compose.yml, örnek configler
tests/      → paket-içi testler (edge/tests, backend/tests, ai/tests)
```

Bağımlılık yönü: `backend → ai` (import eder), `edge → (hiçbiri)`, `ai → (hiçbiri)`, `dashboard → backend API`.

## 2. Olay Sözleşmesi (JSON, schema_version=3)

Tel formatı `docs/02-sistem-mimarisi.md §6` ile birebir. 5 olay tipi: `track_update`,
`zone_enter`, `zone_exit`, `interaction_detected`, `queue_measurement`. Her mesajda zorunlu zarf:

```json
{ "tenant_id": "t_demo", "store_id": 1, "device_id": "edge-1a",
  "schema_version": 3, "seq_no": 42, "event_id": "<uuid4>",
  "event_time": "<RFC3339>", "ingest_time": null }
```

- `seq_no`: cihaz başına monoton artan; backend boşluk görürse `coverage_gaps` kaydı açar.
- `event_id`: idempotens anahtarı — backend aynı `event_id`'yi sessizce yok sayar (200 döner).
- `zone_exit.classification`: `dwell` (≥ eşik, varsayılan 5 sn) | `pass_by`.
- `interaction_detected.interaction`: MVP'de yalnız `interaction_candidate` (pickup/putback rezerve).
- `queue_measurement`: kuyruk zone'larında 30 sn periyot.
- `quality` alt nesnesi her olayda: `conf` (0-1), `coverage_ok` (bool); zone olaylarında + `id_switch_risk`.
- `track_update.is_staff`: bool (BLE beacon simülasyonu / config'te staff track oranı).

İngest isteği: `POST /v1/ingest/events` gövdesi `{"events": [<olay>, ...]}` (≤500 olay/batch).
Yanıt: `{"accepted": n, "duplicates": m, "gap_detected": bool}`.

## 3. Veri Modeli (SQLAlchemy tabloları)

`tenant(id, name, isolation_tier)` · `store(id, tenant_id, name, plan_width_m, plan_height_m, timezone)`
· `edge_device(id, store_id, name, last_heartbeat, health_json)` · `camera(id, store_id, device_id, name, homography_json, reproj_error_cm)`
· `zone(id, store_id, name, zone_type[entrance|shelf|queue|checkout|fitting_room|other], polygon_json, category)`
· `event_raw(event_id PK, tenant_id, store_id, device_id, seq_no, type, event_time, payload_json)` — ham olay arşivi
· `track_position(store_id, camera_id, track_id, ts, x_m, y_m, sigma_cm, is_staff, conf)` — 1 Hz konumlar
· `zone_visit(id, store_id, zone_id, track_id, enter_ts, exit_ts, dwell_sec, classification, is_staff)` — enter+exit eşlenmiş
· `queue_sample(store_id, zone_id, ts, queue_len, est_wait_sec, joins, abandons, active_checkouts)`
· `coverage_gap(id, store_id, device_id, gap_start, gap_end, missing_seq_from, missing_seq_to, reason)`
· `pos_daily(store_id, date, transactions, revenue)` — CSV import
· `briefing(id, store_id, date, text_md, provider, metric_refs_json)`

## 4. Backend API (FastAPI, hepsi `/v1`, JWT `tenant` claim — demo: `Authorization: Bearer demo` kabul edilir)

| Uç | Dönen |
|---|---|
| `POST /v1/ingest/events` | bkz. §2 |
| `GET /v1/stores` · `GET /v1/stores/{id}` | mağaza listesi/detayı (zone'lar dahil) |
| `GET /v1/stores/{id}/metrics?metric=footfall\|occupancy\|conversion&granularity=1h\|1d&from&to` | `{series:[{ts,value}], quality_badge, quality_detail}` |
| `GET /v1/stores/{id}/zones/{zid}/dwell?stat=p50,p95&from&to` | dwell istatistikleri + ziyaret sayısı + draw rate |
| `GET /v1/stores/{id}/heatmap?from&to&cell_m=0.5&kind=density\|dwell` | `{cells:[{x,y,value}], k_suppressed}` — k<10 hücre bastırılır |
| `GET /v1/stores/{id}/paths/first-destination?from&to` | girişten ilk gidilen zone dağılımı |
| `GET /v1/stores/{id}/paths/transitions?from&to` | zone→zone geçiş matrisi (k<10 bastırmalı) |
| `GET /v1/stores/{id}/queues/{zid}/live` | son ölçüm + son 1 saat serisi; `alert: bool` (eşik: `queue_len≥5` veya `est_wait_sec≥300`) |
| `GET /v1/stores/{id}/coverage-gaps?from&to` | boşluk listesi + toplam dakika |
| `GET /v1/stores/{id}/funnel?from&to` | giren → zone'a uğrayan → ilgilenen (interaction) → satın alan (pos_daily) |
| `POST /v1/stores/{id}/pos-import` | CSV (`date,transactions,revenue`) |
| `GET /v1/stores/{id}/briefing?date=` | varsa DB'den; yoksa üretir (ai katmanı) |
| `POST /v1/stores/{id}/assistant` | `{question}` → `{answer_md, metrics_used}` |
| `GET /v1/admin/devices/{id}/health` | heartbeat + sağlık |

Her metrik yanıtında **kalite bağlamı** zorunlu: `quality_badge: green|yellow|red`
(kural: kapsam boşluğu penceresinin >%2'si → yellow, >%10 → red; veri yoksa red) + `quality_detail`.

## 5. AI Katmanı (`wherugo_ai`)

```python
class LLMProvider(Protocol):
    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str: ...
# Uygulamalar: OpenAICompatProvider(base_url, model, api_key)  ← MiMo/vLLM, OpenRouter, yerel
#              AnthropicProvider(model)                        ← ANTHROPIC_API_KEY
#              MockProvider()                                  ← anahtar yoksa: şablon-tabanlı deterministik Türkçe çıktı
# Seçim: env WHERUGO_LLM_PROVIDER=mock|openai|anthropic (varsayılan mock), WHERUGO_LLM_BASE_URL, WHERUGO_LLM_MODEL
```

- `briefing.generate(metrics_bundle: dict, store_name: str, date: str, provider) -> BriefingResult(text_md, metric_refs)`
  — Türkçe günlük brifing; her sayısal iddia `metrics_bundle`'daki bir anahtara referans verir; halüsinasyon
  korkuluğu: bundle'da olmayan sayı yazdırmama talimatı + MockProvider'da yalnız şablon.
- `assistant.answer(question: str, metrics_bundle: dict, provider) -> AnswerResult(answer_md, metrics_used)`
- `metrics_bundle`'ı backend kurar (`backend/wherugo_backend/insights.py`): footfall/dwell/kuyruk/funnel/gap özetleri.
- MiMo-VL klip hakemliği MVP'de **stub** (`vlm.py`: arayüz + MockVLM) — gerçek entegrasyon Jetson sahası ister.

## 6. Kenar (`wherugo_edge`)

- CLI: `wherugo-edge --config <yaml> [--speed 10] [--duration 3600]`
- Config YAML: store/device kimlikleri, backend URL, kameralar (homografi 3×3 matris), zone poligonları
  (plan koordinatı, metre), `staff_ratio`, kuyruk parametreleri. Örnek: `deploy/edge-demo.yaml`
  (~8 zone'lu 20×12 m moda mağazası: giriş, 4 reyon, deneme kabini önü, kasa+kuyruk).
- `sources/simulate.py`: ajan-tabanlı sentetik müşteri üreteci — Poisson varış (saat-of-day eğrisi),
  ilgi profiline göre zone rotası, zone içinde dwell (lognormal), kuyruğa katılma/terk (sabırsızlık eşiği),
  personel track'leri (`is_staff=true`). Determinizm: `--seed`.
- `sources/video.py`: `[cv]` extra kuruluysa RTSP/dosya + ultralytics takibi; kurulu değilse anlaşılır hata.
- `zones.py`: kaynaktan bağımsız zone motoru — nokta-poligon testi, enter/exit histerezisi (2 ardışık örnek),
  dwell eşiği 5 sn, kuyruk zone'unda 30 sn'lik `queue_measurement` üretimi.
- `publisher.py`: bellek + SQLite spool; backend erişilemezse biriktirir, dönünce sırayla boşaltır (at-least-once).
- **Gizlilik değişmezi kodda:** olay üretim yolunda frame/piksel taşıyan hiçbir tip yoktur; `video.py`
  frame'leri yalnız yerel işler, publisher'a yalnız `events.py` tipleri girer.

## 7. Dashboard (statik SPA, `dashboard/`)

Backend `GET /` → `dashboard/index.html`. Vanilla JS + ECharts (CDN; yüklenemezse tablo fallback).
Görünümler: (1) Genel Bakış — footfall/occupancy/conversion kartları + kalite rozeti; (2) Heatmap —
zemin planı üstüne yoğunluk/dwell grid'i + zone çizimleri; (3) Zone analitiği — dwell p50/p95, draw rate,
first-destination; (4) Kuyruk — canlı uzunluk/bekleme + alarm bandı; (5) Brifing — günlük AI brifingi +
asistan soru kutusu. Türkçe arayüz. `fetch('/v1/...', {headers:{Authorization:'Bearer demo'}})`.

## 8. Çalıştırma Hedefi (kabul kriteri)

```bash
make demo   # = backend'i başlat (uvicorn :8000), edge'i simulate modda 10× hızda başlat,
            #   http://localhost:8000 dashboard'unda ~2 dk içinde canlı metrik görünür
make test   # pytest: edge zone motoru, backend ingest idempotensi + metrik hesapları, ai mock brifing
docker compose -f deploy/docker-compose.yml up  # aynı demoyu 2 konteynerde
```

Python ≥3.10, her paket kendi `pyproject.toml`'u ile `pip install -e` kurulabilir; kök `Makefile` üçünü kurar.

---

# v2 Eklentileri — Tam Ürün Sözleşmeleri

## 9. Kimlik Doğrulama (auth v2)

- `WHERUGO_AUTH_MODE=demo|jwt` (varsayılan **demo**: `Bearer demo` → t_demo, mevcut davranış).
- **jwt modu:** HS256 (stdlib hmac — ek bağımlılık yok), secret `WHERUGO_JWT_SECRET`, claim'ler `{"tenant": str, "exp": int}`.
- `POST /v1/auth/token` gövde `{"api_key": "..."}` → `{"token": "...", "expires_in": 86400}`. `tenant.api_key_hash`
  (sha256) ile eşleşir; demo seed t_demo için `WHERUGO_DEMO_API_KEY` (vars. `demo-api-key`) hash'ler.
- 401 gövdesi `{"detail": "..."}`; dashboard 401 görünce token giriş ekranı gösterir (localStorage'da saklar).

## 10. Yönetim API'si (onboarding kod gerektirmez)

| Uç | Gövde/Dönüş |
|---|---|
| `POST /v1/stores` | `{name, plan_width_m, plan_height_m, timezone}` → store (tenant auth'tan) |
| `PUT /v1/stores/{id}` | kısmi güncelleme |
| `POST /v1/stores/{id}/zones` | `{name, zone_type, polygon, category?}` → zone (id server verir) |
| `PUT /v1/stores/{id}/zones/{zid}` · `DELETE ...` | güncelle / sil (silinen zone'un geçmiş verisi kalır) |
| `POST /v1/stores/{id}/devices` | `{id, name}` → edge_device |
| `GET /v1/stores/{id}/devices` | cihaz listesi + sağlık özeti |

Not: Zone değişikliği MVP'de kenara OTOMATİK yansımaz (kenar YAML okur); üretim yolu imzalı config push'tur (docs/06 §6.6). Bu kısıt API açıklamasında belirtilir.

## 11. Alarmlar ve Webhook

- Tablo `alert(id, store_id, zone_id, type, ts, payload_json, delivered_bool)`; `type ∈ {queue_length, queue_wait}`.
- Kuyruk ölçümü alert eşiğini AŞAĞIDAN YUKARI geçtiğinde (kenar durumundan bağımsız, backend ingest'te tespit)
  bir alert kaydı açılır; `store.webhook_url` doluysa `POST {type, store_id, zone_id, ts, queue_len, est_wait_sec}`
  arka planda denenir (timeout 5 sn, başarısızlık alert'i silmez, delivered=false kalır). Histerezis: aynı zone'da
  eşik altına inmeden ikinci alert açılmaz.
- `GET /v1/stores/{id}/alerts?from&to` → liste. `PUT /v1/stores/{id}` webhook_url alanını kabul eder.

## 12. Export

- `GET /v1/stores/{id}/export/rollup?from&to&format=csv` → **saatlik zone rollup** CSV
  (`hour, zone_id, zone_name, visits, unique_visitors, dwell_p50, dwell_p95, queue_max, queue_abandons`).
  k<10 satırlar bastırılır (unique_visitors<10 → dwell alanları boş). Ham track verisi ASLA export edilmez.

## 13. MQTT Taşıma (üretim yolu)

- Edge config: `publisher.transport: http|mqtt` (vars http). mqtt ayarları: `{host, port: 1883, qos: 1}`;
  topic `t/{tenant_id}/{store_id}/{device_id}/events`; payload mevcut JSON batch `{"events": [...]}`.
  paho-mqtt `edge[mqtt]` extra'sı; spool/at-least-once davranışı taşımadan bağımsız aynı.
- Backend köprüsü: `python -m wherugo_backend.mqtt_bridge` — `t/+/+/+/events` abone olur, process_batch'e verir
  (idempotens aynı). Env: `WHERUGO_MQTT_HOST/PORT`. `backend[mqtt]` extra'sı.
- compose `--profile mqtt`: eclipse-mosquitto servisi + bridge + edge'in mqtt transport'u.

## 14. Günlük Otomatik Brifing

- Backend lifespan'da asyncio görevi: her gün `WHERUGO_BRIEFING_HOUR` (vars. 07, store yerel saati) geçildiğinde
  önceki günün brifingini olmayan mağazalar için üretir. `WHERUGO_BRIEFING_AUTO=0` kapatır. Test için fonksiyon
  saf çağrılabilir: `generate_due_briefings(session_factory, now)`.

## 15. VLM İstemcisi (MiMo-VL gerçek yol)

- `ai/wherugo_ai/vlm.py`: `OpenAICompatVLM(base_url, model, api_key=None)` — OpenAI-uyumlu vision chat
  (`image_url` data: base64 jpeg kareleri, en fazla 8 kare) → `VLMVerdict(label, conf, rationale)`;
  yanıt JSON parse korkuluklu. Env fabrika: `get_vlm()` (`WHERUGO_VLM_BASE_URL/MODEL/API_KEY`; yoksa MockVLM).
- Edge video modunda interaction anında son 10 sn'den 8 jpeg karesi `clips/<tarih>/evt-<seq>/` altına yazılır
  (yalnız YEREL disk; 72 saat TTL temizliği edge'de). VLM çağrısı `WHERUGO_VLM_BASE_URL` doluysa kenardan yapılır,
  verdict olayla birlikte gönderilir (`interaction_detected.vlm_verdict/vlm_conf` alanları — backend zaten saklıyor).
- Simülatör klip üretmez (piksel yok); bu yol yalnız video kaynağında.

## 16. Video Kaynağı (GPU'suz test edilebilir)

- `edge[cv]` extra: `opencv-python-headless` (torch DEĞİL). Dedektör soyutlaması: `detector: yolo|mock`.
  `yolo` ultralytics ister (`edge[yolo]` extra, sahada). `mock` dedektör: arka plan çıkarımı + kontur (saf OpenCV)
  — sentetik/test videolarında kişi-vari blob'ları tespit eder; birim testleri sentetik hareketli kare üretip
  video yolunu uçtan uca (frame→track→zone olayı) GPU'suz doğrular.

## 17. CI

- `.github/workflows/ci.yml`: push/PR'da 3 paketin pytest'i + `node --input-type=module --check` dashboard kontrolü.
