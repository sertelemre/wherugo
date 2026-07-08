"""Ingest pipeline: batch events -> event_raw + typed projections.

Guarantees (CONTRACTS section 2):
- at-least-once friendly: duplicate event_id is silently counted, 200 returned;
  concurrent retries of the same batch are absorbed by retrying on
  IntegrityError instead of surfacing a 500
- per-device seq_no gap detection -> coverage_gap(reason='seq_gap'); a seq_no
  that FALLS BELOW the device's high-water mark is treated as a device/counter
  reset -> coverage_gap(reason='seq_reset') and gap tracking resumes from the
  new base
- tenant isolation: an event is only projected when its store_id belongs to
  the authenticated tenant; event_raw.tenant_id always comes from auth
- a malformed event is rejected individually (counted in `rejected`) and never
  drops the rest of the batch
- projections: track_update -> track_position, zone_enter/zone_exit -> zone_visit
  (enter opens an open visit, exit closes it; a lone exit still records a visit),
  queue_measurement -> queue_sample, interaction_detected -> event_raw only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import alerts
from .models import CoverageGap, EdgeDevice, EventRaw, QueueSample, Store, TrackPosition, ZoneVisit
from .util import parse_ts, utcnow

ENVELOPE_FIELDS = ("tenant_id", "store_id", "device_id", "schema_version",
                   "seq_no", "event_id", "event_time", "ingest_time")

# dwell_sec sanity bounds: negative or >24h values are sensor garbage and are
# rejected (they would poison p50/p95 and create future-dated enter_ts rows).
MAX_DWELL_SEC = 86400.0

# Exceptions a single malformed event may raise inside _project / EventRaw
# construction; anything else (IntegrityError, DB errors) must propagate.
_EVENT_DATA_ERRORS = (KeyError, TypeError, ValueError, AttributeError)


@dataclass
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    gap_detected: bool = False
    # Webhook jobs for alerts opened by this batch (CONTRACTS section 11).
    # The caller (ingest endpoint) schedules delivery in the background AFTER
    # the batch commit; entries with url=None are informational only.
    webhook_jobs: list[dict[str, Any]] = field(default_factory=list)


def normalize_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Accept both nested-envelope (docs/02 §6) and flat (CONTRACTS §2) forms."""
    env_src = raw.get("envelope") if isinstance(raw.get("envelope"), dict) else raw
    ev: dict[str, Any] = {k: v for k, v in raw.items() if k != "envelope"}
    for f in ENVELOPE_FIELDS:
        if f in env_src:
            ev[f] = env_src[f]
    required = ("event_id", "device_id", "store_id", "seq_no", "type", "event_time")
    if any(ev.get(f) is None for f in required):
        return None
    ts = parse_ts(ev["event_time"])
    if ts is None:
        return None
    ev["event_time"] = ts
    try:
        ev["seq_no"] = int(ev["seq_no"])
        ev["store_id"] = int(ev["store_id"])
    except (TypeError, ValueError):
        return None
    return ev


def _resolve_is_staff(session: Session, store_id: int, track_id: int | None, ev: dict[str, Any]) -> bool:
    if "is_staff" in ev:
        return bool(ev["is_staff"])
    if track_id is None:
        return False
    val = session.scalar(
        select(TrackPosition.is_staff)
        .where(TrackPosition.store_id == store_id, TrackPosition.track_id == track_id)
        .order_by(TrackPosition.ts.desc())
        .limit(1)
    )
    return bool(val) if val is not None else False


def _project(session: Session, ev: dict[str, Any]) -> dict[str, Any] | None:
    """Project one event into its typed table. Returns a webhook job dict when
    a queue_measurement opened a new alert (see alerts.maybe_open_alert)."""
    etype = ev["type"]
    store_id = ev["store_id"]
    ts: datetime = ev["event_time"]
    quality = ev.get("quality") or {}
    track_id = ev.get("track_id")

    if etype == "track_update":
        pos = ev.get("pos") or {}
        session.add(TrackPosition(
            store_id=store_id,
            camera_id=ev.get("camera_id"),
            track_id=int(track_id) if track_id is not None else 0,
            ts=ts,
            x_m=float(pos.get("x_m", ev.get("x_m", 0.0))),
            y_m=float(pos.get("y_m", ev.get("y_m", 0.0))),
            sigma_cm=pos.get("sigma_cm", ev.get("sigma_cm")),
            is_staff=bool(ev.get("is_staff", False)),
            conf=quality.get("conf"),
        ))

    elif etype == "zone_enter":
        zone_id = ev.get("zone_id")
        if zone_id is None or track_id is None:
            return
        session.add(ZoneVisit(
            store_id=store_id,
            zone_id=int(zone_id),
            track_id=int(track_id),
            enter_ts=ts,
            exit_ts=None,
            dwell_sec=None,
            classification=None,
            is_staff=_resolve_is_staff(session, store_id, int(track_id), ev),
        ))
        session.flush()

    elif etype == "zone_exit":
        zone_id = ev.get("zone_id")
        if zone_id is None or track_id is None:
            return
        # Coerce/validate BEFORE touching any ORM state: a rejected event must
        # not leave a half-mutated open visit behind.
        zone_id, track_id = int(zone_id), int(track_id)
        dwell = ev.get("dwell_sec")
        if dwell is not None:
            dwell = float(dwell)
            if not (0.0 <= dwell <= MAX_DWELL_SEC):
                raise ValueError(f"dwell_sec out of range [0, {MAX_DWELL_SEC}]: {dwell}")
        classification = ev.get("classification")
        open_visit = session.scalars(
            select(ZoneVisit)
            .where(ZoneVisit.store_id == store_id,
                   ZoneVisit.zone_id == zone_id,
                   ZoneVisit.track_id == track_id,
                   ZoneVisit.exit_ts == None)  # noqa: E711
            .order_by(ZoneVisit.enter_ts.desc())
            .limit(1)
        ).first()
        if open_visit is not None:
            open_visit.exit_ts = ts
            open_visit.dwell_sec = dwell if dwell is not None else max(
                (ts - open_visit.enter_ts).total_seconds(), 0.0)
            open_visit.classification = classification or (
                "dwell" if (open_visit.dwell_sec or 0) >= 5.0 else "pass_by")
        else:
            # Lone exit (enter was lost): still record the visit. dwell is
            # already validated >= 0, so enter_ts can never land in the future.
            dwell_f = dwell if dwell is not None else 0.0
            session.add(ZoneVisit(
                store_id=store_id,
                zone_id=zone_id,
                track_id=track_id,
                enter_ts=ts - timedelta(seconds=dwell_f),
                exit_ts=ts,
                dwell_sec=dwell_f,
                classification=classification or ("dwell" if dwell_f >= 5.0 else "pass_by"),
                is_staff=_resolve_is_staff(session, store_id, track_id, ev),
            ))
        session.flush()

    elif etype == "queue_measurement":
        zone_id = ev.get("zone_id")
        if zone_id is None:
            return None
        zone_id = int(zone_id)
        queue_len = int(ev.get("queue_len", 0))
        est_wait_raw = ev.get("est_wait_sec")
        est_wait = float(est_wait_raw) if est_wait_raw is not None else None
        # Alert rising-edge check MUST run before the new sample is inserted:
        # the "previous state" is the latest stored sample of this zone.
        job = alerts.maybe_open_alert(session, store_id, zone_id, ts, queue_len, est_wait)
        session.add(QueueSample(
            store_id=store_id,
            zone_id=zone_id,
            ts=ts,
            queue_len=queue_len,
            est_wait_sec=est_wait,
            joins=int(ev.get("joins_since_last", ev.get("joins", 0)) or 0),
            abandons=int(ev.get("abandons_since_last", ev.get("abandons", 0)) or 0),
            active_checkouts=int(ev.get("active_checkouts", 0) or 0),
        ))
        return job

    # interaction_detected: kept in event_raw only (funnel reads it from there).
    return None


def _detect_gaps(session: Session, device_id: str, store_id: int,
                 events: list[dict[str, Any]]) -> bool:
    """Per-device monotonic seq_no check; writes coverage_gap rows for holes.

    A seq_no BELOW the device's current stream tail means the device counter
    was reset (edge spool wiped / container recreated): a coverage_gap with
    reason='seq_reset' is recorded and the baseline moves to the new stream, so
    genuine holes after the reset keep being detected (previously gap tracking
    silently died until seq passed the historic max).

    The baseline is the seq_no of the device's LATEST event (by event_time),
    not the all-time max(seq_no): after a reset the all-time max would flag
    every following batch as a new reset and lose the rebased tracking."""
    row = session.execute(
        select(EventRaw.seq_no, EventRaw.event_time)
        .where(EventRaw.device_id == device_id)
        .order_by(EventRaw.event_time.desc(), EventRaw.seq_no.desc())
        .limit(1)
    ).first()
    last_seq: int | None = row[0] if row else None
    prev_time: datetime | None = row[1] if row else None
    gap_found = False
    for ev in sorted(events, key=lambda e: e["seq_no"]):
        seq = ev["seq_no"]
        if last_seq is not None and seq < last_seq:
            # Device counter reset: unknown coverage between the old stream's
            # tail and this event; rebase gap tracking onto the new stream.
            session.add(CoverageGap(
                store_id=store_id,
                device_id=device_id,
                gap_start=prev_time or ev["event_time"],
                gap_end=ev["event_time"],
                missing_seq_from=None,
                missing_seq_to=None,
                reason="seq_reset",
            ))
            gap_found = True
            last_seq = seq
            prev_time = ev["event_time"]
            continue
        if last_seq is not None and seq > last_seq + 1:
            session.add(CoverageGap(
                store_id=store_id,
                device_id=device_id,
                gap_start=prev_time or ev["event_time"],
                gap_end=ev["event_time"],
                missing_seq_from=last_seq + 1,
                missing_seq_to=seq - 1,
                reason="seq_gap",
            ))
            gap_found = True
        if last_seq is None or seq > last_seq:
            last_seq = seq
            prev_time = ev["event_time"]
    return gap_found


def _touch_device(session: Session, device_id: str, store_id: int) -> None:
    device = session.get(EdgeDevice, device_id)
    if device is None:
        session.add(EdgeDevice(id=device_id, store_id=store_id, name=device_id,
                               last_heartbeat=utcnow()))
    else:
        device.last_heartbeat = utcnow()


def process_batch(session: Session, tenant_id: str, raw_events: list[dict[str, Any]]) -> IngestResult:
    """Idempotent batch ingest. Retries on IntegrityError so a concurrent
    retry of the same batch (edge timeout + re-POST while the first request is
    still in flight) resolves to duplicates instead of an HTTP 500."""
    attempts = 3
    for attempt in range(attempts):
        try:
            return _process_batch_once(session, tenant_id, raw_events)
        except IntegrityError:
            session.rollback()
            if attempt == attempts - 1:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def _process_batch_once(session: Session, tenant_id: str, raw_events: list[dict[str, Any]]) -> IngestResult:
    result = IngestResult()
    normalized: list[dict[str, Any]] = []
    seen_batch_ids: set[str] = set()
    tenant_by_store: dict[int, str | None] = {}

    def store_tenant(store_id: int) -> str | None:
        if store_id not in tenant_by_store:
            store = session.get(Store, store_id)
            tenant_by_store[store_id] = store.tenant_id if store is not None else None
        return tenant_by_store[store_id]

    for raw in raw_events:
        ev = normalize_event(raw)
        if ev is None:
            result.rejected += 1  # malformed envelope: neither accepted nor duplicate
            continue
        if store_tenant(ev["store_id"]) != tenant_id:
            # Unknown store or another tenant's store: never project data
            # across the tenant boundary (CONTRACTS section 3 isolation).
            result.rejected += 1
            continue
        eid = str(ev["event_id"])
        if eid in seen_batch_ids:
            result.duplicates += 1
            continue
        if session.get(EventRaw, eid) is not None:
            result.duplicates += 1
            continue
        seen_batch_ids.add(eid)
        normalized.append(ev)

    # Gap detection runs per device on genuinely new events only,
    # so retried (duplicate) batches never re-open the same gap.
    by_device: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for ev in normalized:
        by_device.setdefault((str(ev["device_id"]), ev["store_id"]), []).append(ev)
    for (device_id, store_id), evs in by_device.items():
        if _detect_gaps(session, device_id, store_id, evs):
            result.gap_detected = True
        _touch_device(session, device_id, store_id)

    for ev in sorted(normalized, key=lambda e: (str(e["device_id"]), e["seq_no"])):
        try:
            # _project validates/coerces before any session mutation, so a
            # data error here leaves no partial state; the event_raw row is
            # only added once the projection succeeded.
            job = _project(session, ev)
            payload = {k: v for k, v in ev.items() if k not in ("event_time",)}
            payload["event_time"] = ev["event_time"].isoformat() + "Z"
            session.add(EventRaw(
                event_id=str(ev["event_id"]),
                tenant_id=tenant_id,  # always the authenticated tenant, never the body's
                store_id=ev["store_id"],
                device_id=str(ev["device_id"]),
                seq_no=ev["seq_no"],
                type=str(ev["type"]),
                event_time=ev["event_time"],
                payload_json=payload,
            ))
            session.flush()
        except _EVENT_DATA_ERRORS:
            result.rejected += 1  # one poisoned event must not drop the batch
            continue
        if job is not None:
            result.webhook_jobs.append(job)
        result.accepted += 1

    session.commit()
    return result
