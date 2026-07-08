"""All /v1 endpoints (CONTRACTS.md section 4). Every metric response carries
quality_badge + quality_detail."""
from __future__ import annotations

import csv
import io
from datetime import date as date_type, datetime, timedelta

from fastapi import (APIRouter, BackgroundTasks, Depends, File, HTTPException, Query,
                     Request, Response, UploadFile)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import alerts as alerts_mod
from . import insights, metrics
from .auth import TOKEN_TTL_SEC, AuthContext, get_auth, hash_api_key, issue_token
from .ingest import process_batch
from .models import Alert, Briefing, EdgeDevice, PosDaily, Store, Tenant, Zone
from .quality import quality_context
from .schemas import (
    MAX_BATCH_SIZE,
    AlertOut,
    AssistantRequest,
    AssistantResponse,
    BriefingResponse,
    CoverageGapsResponse,
    DeviceCreate,
    DeviceHealthResponse,
    DwellResponse,
    FirstDestinationResponse,
    FunnelResponse,
    HeatmapResponse,
    IngestRequest,
    IngestResponse,
    MetricsResponse,
    PosImportResponse,
    QueueLiveResponse,
    StoreCreate,
    StoreOut,
    StoreUpdate,
    TokenRequest,
    TokenResponse,
    TransitionsResponse,
    ZoneCreate,
    ZoneOut,
    ZoneUpdate,
)
from .util import parse_date, parse_window, utcnow

router = APIRouter(prefix="/v1")


def get_db(request: Request):
    session: Session = request.app.state.sessionmaker()
    try:
        yield session
    finally:
        session.close()


def _get_store(db: Session, auth: AuthContext, store_id: int) -> Store:
    store = db.get(Store, store_id)
    if store is None or store.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=404, detail="store not found")
    return store


def _zone_out(z: Zone) -> ZoneOut:
    return ZoneOut(id=z.id, store_id=z.store_id, name=z.name, zone_type=z.zone_type,
                   polygon=z.polygon_json, category=z.category)


def _store_out(db: Session, store: Store) -> StoreOut:
    zones = db.scalars(select(Zone).where(Zone.store_id == store.id).order_by(Zone.id)).all()
    return StoreOut(
        id=store.id, tenant_id=store.tenant_id, name=store.name,
        plan_width_m=store.plan_width_m, plan_height_m=store.plan_height_m,
        timezone=store.timezone, webhook_url=store.webhook_url,
        zones=[_zone_out(z) for z in zones],
    )


def _get_zone(db: Session, store_id: int, zone_id: int) -> Zone:
    zone = db.get(Zone, zone_id)
    if zone is None or zone.store_id != store_id:
        raise HTTPException(status_code=404, detail="zone not found")
    return zone


def _device_out(device: EdgeDevice) -> DeviceHealthResponse:
    hb = device.last_heartbeat
    online = hb is not None and (utcnow() - hb).total_seconds() < 300
    return DeviceHealthResponse(id=device.id, store_id=device.store_id, name=device.name,
                                last_heartbeat=hb.isoformat() + "Z" if hb else None,
                                online=online, health=device.health_json)


# --- auth v2 (CONTRACTS section 9) ----------------------------------------------

@router.post("/auth/token", response_model=TokenResponse)
def auth_token(body: TokenRequest, db: Session = Depends(get_db)):
    """Exchange a tenant API key for an HS256 JWT (claims: tenant, exp).
    The key is matched against tenant.api_key_hash (sha256); a wrong key is 401."""
    api_key = body.api_key or ""
    tenant = db.scalars(
        select(Tenant).where(Tenant.api_key_hash == hash_api_key(api_key)).limit(1)
    ).first() if api_key else None
    if tenant is None:
        raise HTTPException(status_code=401, detail="invalid api key")
    return TokenResponse(token=issue_token(tenant.id), expires_in=TOKEN_TTL_SEC)


# --- ingest -------------------------------------------------------------------

@router.post("/ingest/events", response_model=IngestResponse)
def ingest_events(body: IngestRequest, background_tasks: BackgroundTasks,
                  request: Request, db: Session = Depends(get_db),
                  auth: AuthContext = Depends(get_auth)):
    if len(body.events) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=413, detail=f"batch too large (max {MAX_BATCH_SIZE})")
    result = process_batch(db, auth.tenant_id, body.events)
    # Alert webhooks (CONTRACTS section 11): delivered in the background after
    # the batch commit; a failed delivery keeps the alert with delivered=false.
    for job in result.webhook_jobs:
        if job.get("url"):
            background_tasks.add_task(alerts_mod.deliver_webhook,
                                      request.app.state.sessionmaker,
                                      job["alert_id"], job["url"], job["payload"])
    return IngestResponse(accepted=result.accepted, duplicates=result.duplicates,
                          gap_detected=result.gap_detected, rejected=result.rejected)


# --- stores -------------------------------------------------------------------

@router.get("/stores", response_model=list[StoreOut])
def list_stores(db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    stores = db.scalars(select(Store).where(Store.tenant_id == auth.tenant_id).order_by(Store.id)).all()
    return [_store_out(db, s) for s in stores]


@router.get("/stores/{store_id}", response_model=StoreOut)
def get_store(store_id: int, db: Session = Depends(get_db),
              auth: AuthContext = Depends(get_auth)):
    return _store_out(db, _get_store(db, auth, store_id))


# --- management API (CONTRACTS section 10) ---------------------------------------
#
# NOTE (contract section 10): store/zone changes made here do NOT propagate to
# the edge automatically in the MVP — the edge reads its zones/homography from
# its local YAML config. The production path is a signed config push (docs/06
# section 6.6). Update deploy/edge-demo.yaml (or the device's config) by hand
# after changing zones, otherwise the edge keeps emitting events for the old
# geometry.

@router.post("/stores", response_model=StoreOut, status_code=201)
def create_store(body: StoreCreate, db: Session = Depends(get_db),
                 auth: AuthContext = Depends(get_auth)):
    """Create a store for the authenticated tenant (tenant comes from auth,
    never from the body). NOTE: zones added later are NOT pushed to the edge
    automatically — the edge reads its own YAML config (see section note above)."""
    store = Store(tenant_id=auth.tenant_id, name=body.name,
                  plan_width_m=body.plan_width_m, plan_height_m=body.plan_height_m,
                  timezone=body.timezone, webhook_url=body.webhook_url)
    db.add(store)
    db.commit()
    return _store_out(db, store)


@router.put("/stores/{store_id}", response_model=StoreOut)
def update_store(store_id: int, body: StoreUpdate, db: Session = Depends(get_db),
                 auth: AuthContext = Depends(get_auth)):
    """Partial store update; accepts webhook_url (CONTRACTS section 11). Only
    fields present in the body are changed (webhook_url: null clears it)."""
    store = _get_store(db, auth, store_id)
    data = body.model_dump(exclude_unset=True)
    for field in ("name", "plan_width_m", "plan_height_m", "timezone", "webhook_url"):
        if field in data:
            setattr(store, field, data[field])
    db.commit()
    return _store_out(db, store)


@router.post("/stores/{store_id}/zones", response_model=ZoneOut, status_code=201)
def create_zone(store_id: int, body: ZoneCreate, db: Session = Depends(get_db),
                auth: AuthContext = Depends(get_auth)):
    """Add a zone (id assigned by the server). NOT auto-pushed to the edge:
    the edge simulator/tracker keeps using its local YAML zone polygons."""
    _get_store(db, auth, store_id)
    zone = Zone(store_id=store_id, name=body.name, zone_type=body.zone_type,
                polygon_json=body.polygon, category=body.category)
    db.add(zone)
    db.commit()
    return _zone_out(zone)


@router.put("/stores/{store_id}/zones/{zone_id}", response_model=ZoneOut)
def update_zone(store_id: int, zone_id: int, body: ZoneUpdate,
                db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    """Partial zone update. NOT auto-pushed to the edge (local YAML config)."""
    _get_store(db, auth, store_id)
    zone = _get_zone(db, store_id, zone_id)
    data = body.model_dump(exclude_unset=True)
    if "polygon" in data:
        zone.polygon_json = data.pop("polygon")
    for field in ("name", "zone_type", "category"):
        if field in data:
            setattr(zone, field, data[field])
    db.commit()
    return _zone_out(zone)


@router.delete("/stores/{store_id}/zones/{zone_id}", status_code=204)
def delete_zone(store_id: int, zone_id: int, db: Session = Depends(get_db),
                auth: AuthContext = Depends(get_auth)):
    """Delete a zone definition. Historical data (zone_visit, queue_sample,
    alert rows referencing the zone id) is intentionally KEPT (CONTRACTS
    section 10); only the zone definition disappears from store detail."""
    _get_store(db, auth, store_id)
    zone = _get_zone(db, store_id, zone_id)
    db.delete(zone)
    db.commit()
    return Response(status_code=204)


@router.post("/stores/{store_id}/devices", response_model=DeviceHealthResponse,
             status_code=201)
def create_device(store_id: int, body: DeviceCreate, db: Session = Depends(get_db),
                  auth: AuthContext = Depends(get_auth)):
    """Register an edge device (id chosen by the caller, e.g. 'edge-1a')."""
    _get_store(db, auth, store_id)
    if db.get(EdgeDevice, body.id) is not None:
        raise HTTPException(status_code=409, detail="device already exists")
    device = EdgeDevice(id=body.id, store_id=store_id, name=body.name or body.id)
    db.add(device)
    db.commit()
    return _device_out(device)


@router.get("/stores/{store_id}/devices", response_model=list[DeviceHealthResponse])
def list_devices(store_id: int, db: Session = Depends(get_db),
                 auth: AuthContext = Depends(get_auth)):
    """Device list with health summary (same shape as /v1/admin/devices/{id}/health)."""
    _get_store(db, auth, store_id)
    devices = db.scalars(select(EdgeDevice).where(EdgeDevice.store_id == store_id)
                         .order_by(EdgeDevice.id)).all()
    return [_device_out(d) for d in devices]


# --- alerts (CONTRACTS section 11) -------------------------------------------------

@router.get("/stores/{store_id}/alerts", response_model=list[AlertOut])
def store_alerts(store_id: int,
                 from_: str | None = Query(None, alias="from"),
                 to: str | None = Query(None),
                 db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    _get_store(db, auth, store_id)
    t_from, t_to = parse_window(from_, to)
    rows = db.scalars(
        select(Alert).where(Alert.store_id == store_id,
                            Alert.ts >= t_from, Alert.ts <= t_to)
        .order_by(Alert.ts, Alert.id)
    ).all()
    out = []
    for a in rows:
        payload = a.payload_json or {}
        out.append(AlertOut(id=a.id, store_id=a.store_id, zone_id=a.zone_id,
                            type=a.type, ts=a.ts.isoformat() + "Z",
                            queue_len=payload.get("queue_len"),
                            est_wait_sec=payload.get("est_wait_sec"),
                            delivered=a.delivered))
    return out


# --- export (CONTRACTS section 12) ---------------------------------------------------

@router.get("/stores/{store_id}/export/rollup")
def export_rollup(store_id: int,
                  from_: str | None = Query(None, alias="from"),
                  to: str | None = Query(None),
                  format: str = Query("csv", pattern="^csv$"),
                  db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    """Hourly zone rollup as CSV (attachment). k-anonymity: rows with
    unique_visitors<10 keep counts but their dwell fields are blank. Raw track
    data is NEVER exported."""
    _get_store(db, auth, store_id)
    t_from, t_to = parse_window(from_, to)
    rows = metrics.hourly_zone_rollup(db, store_id, t_from, t_to)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["hour", "zone_id", "zone_name", "visits", "unique_visitors",
                     "dwell_p50", "dwell_p95", "queue_max", "queue_abandons"])
    for r in rows:
        writer.writerow([
            r["hour"], r["zone_id"], r["zone_name"], r["visits"], r["unique_visitors"],
            "" if r["dwell_p50"] is None else r["dwell_p50"],
            "" if r["dwell_p95"] is None else r["dwell_p95"],
            "" if r["queue_max"] is None else r["queue_max"],
            "" if r["queue_abandons"] is None else r["queue_abandons"],
        ])
    filename = f"wherugo-rollup-store{store_id}-{t_from:%Y%m%d%H}-{t_to:%Y%m%d%H}.csv"
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# --- metrics -------------------------------------------------------------------

@router.get("/stores/{store_id}/metrics", response_model=MetricsResponse)
def store_metrics(store_id: int,
                  metric: str = Query("footfall", pattern="^(footfall|occupancy|conversion)$"),
                  granularity: str = Query("1h", pattern="^(1h|1d)$"),
                  from_: str | None = Query(None, alias="from"),
                  to: str | None = Query(None),
                  db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    _get_store(db, auth, store_id)
    t_from, t_to = parse_window(from_, to)
    fn = {"footfall": metrics.footfall, "occupancy": metrics.occupancy,
          "conversion": metrics.conversion}[metric]
    data = fn(db, store_id, t_from, t_to, granularity)
    badge, detail = quality_context(db, store_id, t_from, t_to,
                                    has_data=data["has_data"] or None)
    # The metric may override the effective granularity (conversion is always
    # daily and returns granularity="1d" even when 1h was requested).
    return MetricsResponse(metric=metric,
                           granularity=data.get("granularity", granularity),
                           series=data["series"],
                           total=data["total"], quality_badge=badge, quality_detail=detail)


@router.get("/stores/{store_id}/zones/{zone_id}/dwell", response_model=DwellResponse)
def zone_dwell(store_id: int, zone_id: int,
               stat: str = Query("p50,p95"),
               from_: str | None = Query(None, alias="from"), to: str | None = Query(None),
               db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    _get_store(db, auth, store_id)
    zone = db.get(Zone, zone_id)
    if zone is None or zone.store_id != store_id:
        raise HTTPException(status_code=404, detail="zone not found")
    t_from, t_to = parse_window(from_, to)
    data = metrics.dwell_stats(db, store_id, zone_id, t_from, t_to, stat.split(","))
    badge, detail = quality_context(db, store_id, t_from, t_to,
                                    has_data=data["has_data"] or None)
    return DwellResponse(zone_id=zone_id, zone_name=zone.name, stats=data["stats"],
                         visits=data["visits"], draw_rate=data["draw_rate"],
                         suppressed=data.get("suppressed", False),
                         quality_badge=badge, quality_detail=detail)


@router.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
def store_heatmap(store_id: int,
                  from_: str | None = Query(None, alias="from"), to: str | None = Query(None),
                  cell_m: float = Query(0.5, gt=0.05, le=10.0),
                  kind: str = Query("density", pattern="^(density|dwell)$"),
                  db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    _get_store(db, auth, store_id)
    t_from, t_to = parse_window(from_, to)
    data = metrics.heatmap(db, store_id, t_from, t_to, cell_m=cell_m, kind=kind)
    badge, detail = quality_context(db, store_id, t_from, t_to,
                                    has_data=data["has_data"] or None)
    return HeatmapResponse(kind=kind, cell_m=cell_m, cells=data["cells"],
                           k_suppressed=data["k_suppressed"],
                           quality_badge=badge, quality_detail=detail)


@router.get("/stores/{store_id}/paths/first-destination", response_model=FirstDestinationResponse)
def paths_first_destination(store_id: int,
                            from_: str | None = Query(None, alias="from"),
                            to: str | None = Query(None),
                            db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    _get_store(db, auth, store_id)
    t_from, t_to = parse_window(from_, to)
    data = metrics.first_destination(db, store_id, t_from, t_to)
    badge, detail = quality_context(db, store_id, t_from, t_to,
                                    has_data=data["has_data"] or None)
    return FirstDestinationResponse(distribution=data["distribution"],
                                    total_tracks=data["total_tracks"],
                                    k_suppressed=data["k_suppressed"],
                                    quality_badge=badge, quality_detail=detail)


@router.get("/stores/{store_id}/paths/transitions", response_model=TransitionsResponse)
def paths_transitions(store_id: int,
                      from_: str | None = Query(None, alias="from"), to: str | None = Query(None),
                      db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    _get_store(db, auth, store_id)
    t_from, t_to = parse_window(from_, to)
    data = metrics.transitions(db, store_id, t_from, t_to)
    badge, detail = quality_context(db, store_id, t_from, t_to,
                                    has_data=data["has_data"] or None)
    return TransitionsResponse(matrix=data["matrix"], k_suppressed=data["k_suppressed"],
                               quality_badge=badge, quality_detail=detail)


@router.get("/stores/{store_id}/queues/{zone_id}/live", response_model=QueueLiveResponse)
def queue_live(store_id: int, zone_id: int,
               db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    _get_store(db, auth, store_id)
    zone = db.get(Zone, zone_id)
    if zone is None or zone.store_id != store_id:
        raise HTTPException(status_code=404, detail="zone not found")
    now = utcnow()
    data = metrics.queue_live(db, store_id, zone_id, now)
    badge, detail = quality_context(db, store_id, now - timedelta(hours=1), now,
                                    has_data=data["has_data"] or None)
    return QueueLiveResponse(zone_id=zone_id, latest=data["latest"], series=data["series"],
                             alert=data["alert"], quality_badge=badge, quality_detail=detail)


@router.get("/stores/{store_id}/coverage-gaps", response_model=CoverageGapsResponse)
def store_coverage_gaps(store_id: int,
                        from_: str | None = Query(None, alias="from"),
                        to: str | None = Query(None),
                        db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    _get_store(db, auth, store_id)
    t_from, t_to = parse_window(from_, to)
    data = metrics.coverage_gaps(db, store_id, t_from, t_to)
    badge, detail = quality_context(db, store_id, t_from, t_to)
    return CoverageGapsResponse(gaps=data["gaps"], total_minutes=data["total_minutes"],
                                quality_badge=badge, quality_detail=detail)


@router.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
def store_funnel(store_id: int,
                 from_: str | None = Query(None, alias="from"), to: str | None = Query(None),
                 db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    _get_store(db, auth, store_id)
    t_from, t_to = parse_window(from_, to)
    data = metrics.funnel(db, store_id, t_from, t_to)
    badge, detail = quality_context(db, store_id, t_from, t_to,
                                    has_data=data["has_data"] or None)
    return FunnelResponse(steps=data["steps"], quality_badge=badge, quality_detail=detail)


# --- pos import -----------------------------------------------------------------

@router.post("/stores/{store_id}/pos-import", response_model=PosImportResponse,
             response_model_exclude_none=True)
async def pos_import(store_id: int, file: UploadFile = File(...),
                     db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    _get_store(db, auth, store_id)
    try:
        content = (await file.read()).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422,
                            detail="file must be a UTF-8 encoded CSV (dosya UTF-8 kodlamalı CSV olmalı)")
    reader = csv.reader(io.StringIO(content))
    imported = 0
    skipped = 0
    for row in reader:
        if not row or not row[0].strip():
            continue
        first = row[0].strip().lower()
        if first in ("date", "tarih"):
            continue  # header
        try:
            day = date_type.fromisoformat(row[0].strip())
            transactions = int(float(row[1]))
            revenue = float(row[2]) if len(row) > 2 and row[2].strip() else 0.0
        except (ValueError, IndexError):
            skipped += 1  # bad row: skip and count, do not fail the whole file
            continue
        existing = db.get(PosDaily, (store_id, day))
        if existing:
            existing.transactions = transactions
            existing.revenue = revenue
        else:
            db.add(PosDaily(store_id=store_id, date=day,
                            transactions=transactions, revenue=revenue))
        imported += 1
    db.commit()
    return PosImportResponse(imported=imported, skipped=skipped or None)


# --- ai: briefing / assistant ------------------------------------------------------

_AI_UNAVAILABLE = ("wherugo_ai package is not installed; briefing/assistant "
                   "endpoints are unavailable (install the ai/ package)")


def _briefing_row(db: Session, store_id: int, day) -> Briefing | None:
    """Canonical stored briefing for (store, day): the FIRST one written, so
    every reader converges on the same text even if a race ever produced
    more than one row."""
    return db.scalars(
        select(Briefing).where(Briefing.store_id == store_id, Briefing.date == day)
        .order_by(Briefing.id).limit(1)
    ).first()


def _briefing_response(store_id: int, day, row: Briefing) -> BriefingResponse:
    return BriefingResponse(store_id=store_id, date=day.isoformat(),
                            text_md=row.text_md, provider=row.provider,
                            metric_refs=row.metric_refs_json or [])


@router.get("/stores/{store_id}/briefing", response_model=BriefingResponse)
def store_briefing(store_id: int, date: str | None = Query(None),
                   db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    store = _get_store(db, auth, store_id)
    try:
        day = parse_date(date) or utcnow().date()
    except ValueError:
        raise HTTPException(status_code=422,
                            detail="date must be in YYYY-MM-DD format (ISO 8601)")

    existing = _briefing_row(db, store_id, day)
    if existing:
        return _briefing_response(store_id, day, existing)

    ai = insights.load_ai()
    if ai is None:
        raise HTTPException(status_code=501, detail=_AI_UNAVAILABLE)

    t_from = datetime(day.year, day.month, day.day)
    t_to = t_from + timedelta(days=1)
    bundle = insights.build_metrics_bundle(db, store_id, t_from, t_to)
    try:
        result = ai.briefing.generate(bundle, store.name, day.isoformat(), ai.provider)
    except HTTPException:
        raise
    except Exception as exc:  # ProviderError etc. -> 503, not a raw 500
        raise HTTPException(status_code=503,
                            detail=f"AI sağlayıcısı yanıt vermedi: {exc}") from exc
    text_md = getattr(result, "text_md", None) or (result.get("text_md") if isinstance(result, dict) else "")
    metric_refs = getattr(result, "metric_refs", None) or (
        result.get("metric_refs") if isinstance(result, dict) else []) or []

    # Get-or-create race: a concurrent request may have stored a briefing for
    # the same (store, day) while we were generating — prefer that row so both
    # clients see identical text and no duplicate rows pile up.
    concurrent = _briefing_row(db, store_id, day)
    if concurrent is not None:
        return _briefing_response(store_id, day, concurrent)
    db.add(Briefing(store_id=store_id, date=day, text_md=text_md,
                    provider=ai.provider_name, metric_refs_json=list(metric_refs)))
    try:
        db.commit()
    except IntegrityError:
        # A (store_id, date) unique constraint (e.g. in a PG deployment) fired:
        # someone else won the insert; serve their row.
        db.rollback()
        winner = _briefing_row(db, store_id, day)
        if winner is not None:
            return _briefing_response(store_id, day, winner)
        raise
    return BriefingResponse(store_id=store_id, date=day.isoformat(), text_md=text_md,
                            provider=ai.provider_name, metric_refs=list(metric_refs))


@router.post("/stores/{store_id}/assistant", response_model=AssistantResponse)
def store_assistant(store_id: int, body: AssistantRequest,
                    db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth)):
    _get_store(db, auth, store_id)
    ai = insights.load_ai()
    if ai is None:
        raise HTTPException(status_code=501, detail=_AI_UNAVAILABLE)
    t_to = utcnow()
    t_from = t_to - timedelta(hours=24)
    bundle = insights.build_metrics_bundle(db, store_id, t_from, t_to)
    try:
        result = ai.assistant.answer(body.question, bundle, ai.provider)
    except HTTPException:
        raise
    except Exception as exc:  # ProviderError etc. -> 503, not a raw 500
        raise HTTPException(status_code=503,
                            detail=f"AI sağlayıcısı yanıt vermedi: {exc}") from exc
    answer_md = getattr(result, "answer_md", None) or (
        result.get("answer_md") if isinstance(result, dict) else "")
    metrics_used = getattr(result, "metrics_used", None) or (
        result.get("metrics_used") if isinstance(result, dict) else []) or []
    return AssistantResponse(answer_md=answer_md, metrics_used=list(metrics_used))


# --- admin ---------------------------------------------------------------------------

@router.get("/admin/devices/{device_id}/health", response_model=DeviceHealthResponse)
def device_health(device_id: str, db: Session = Depends(get_db),
                  auth: AuthContext = Depends(get_auth)):
    device = db.get(EdgeDevice, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    store = db.get(Store, device.store_id)
    if store is None or store.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=404, detail="device not found")
    return _device_out(device)
