"""All /v1 endpoints (CONTRACTS.md section 4). Every metric response carries
quality_badge + quality_detail."""
from __future__ import annotations

import csv
import io
from datetime import date as date_type, datetime, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import insights, metrics
from .auth import AuthContext, get_auth
from .ingest import process_batch
from .models import Briefing, EdgeDevice, PosDaily, Store, Zone
from .quality import quality_context
from .schemas import (
    MAX_BATCH_SIZE,
    AssistantRequest,
    AssistantResponse,
    BriefingResponse,
    CoverageGapsResponse,
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
    StoreOut,
    TransitionsResponse,
    ZoneOut,
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


def _store_out(db: Session, store: Store) -> StoreOut:
    zones = db.scalars(select(Zone).where(Zone.store_id == store.id).order_by(Zone.id)).all()
    return StoreOut(
        id=store.id, tenant_id=store.tenant_id, name=store.name,
        plan_width_m=store.plan_width_m, plan_height_m=store.plan_height_m,
        timezone=store.timezone,
        zones=[ZoneOut(id=z.id, store_id=z.store_id, name=z.name, zone_type=z.zone_type,
                       polygon=z.polygon_json, category=z.category) for z in zones],
    )


# --- ingest -------------------------------------------------------------------

@router.post("/ingest/events", response_model=IngestResponse)
def ingest_events(body: IngestRequest, db: Session = Depends(get_db),
                  auth: AuthContext = Depends(get_auth)):
    if len(body.events) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=413, detail=f"batch too large (max {MAX_BATCH_SIZE})")
    result = process_batch(db, auth.tenant_id, body.events)
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
    hb = device.last_heartbeat
    online = hb is not None and (utcnow() - hb).total_seconds() < 300
    return DeviceHealthResponse(id=device.id, store_id=device.store_id, name=device.name,
                                last_heartbeat=hb.isoformat() + "Z" if hb else None,
                                online=online, health=device.health_json)
