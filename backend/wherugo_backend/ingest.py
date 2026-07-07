"""Ingest pipeline: batch events -> event_raw + typed projections.

Guarantees (CONTRACTS section 2):
- at-least-once friendly: duplicate event_id is silently counted, 200 returned
- per-device seq_no gap detection -> coverage_gap(reason='seq_gap')
- projections: track_update -> track_position, zone_enter/zone_exit -> zone_visit
  (enter opens an open visit, exit closes it; a lone exit still records a visit),
  queue_measurement -> queue_sample, interaction_detected -> event_raw only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import CoverageGap, EdgeDevice, EventRaw, QueueSample, TrackPosition, ZoneVisit
from .util import parse_ts, utcnow

ENVELOPE_FIELDS = ("tenant_id", "store_id", "device_id", "schema_version",
                   "seq_no", "event_id", "event_time", "ingest_time")


@dataclass
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    gap_detected: bool = False


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


def _project(session: Session, ev: dict[str, Any]) -> None:
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
        zone_id, track_id = int(zone_id), int(track_id)
        dwell = ev.get("dwell_sec")
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
            open_visit.dwell_sec = float(dwell) if dwell is not None else max(
                (ts - open_visit.enter_ts).total_seconds(), 0.0)
            open_visit.classification = classification or (
                "dwell" if (open_visit.dwell_sec or 0) >= 5.0 else "pass_by")
        else:
            # Lone exit (enter was lost): still record the visit.
            dwell_f = float(dwell) if dwell is not None else 0.0
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
            return
        session.add(QueueSample(
            store_id=store_id,
            zone_id=int(zone_id),
            ts=ts,
            queue_len=int(ev.get("queue_len", 0)),
            est_wait_sec=ev.get("est_wait_sec"),
            joins=int(ev.get("joins_since_last", ev.get("joins", 0)) or 0),
            abandons=int(ev.get("abandons_since_last", ev.get("abandons", 0)) or 0),
            active_checkouts=int(ev.get("active_checkouts", 0) or 0),
        ))

    # interaction_detected: kept in event_raw only (funnel reads it from there).


def _detect_gaps(session: Session, device_id: str, store_id: int,
                 events: list[dict[str, Any]]) -> bool:
    """Per-device monotonic seq_no check; writes coverage_gap rows for holes."""
    last_seq = session.scalar(
        select(func.max(EventRaw.seq_no)).where(EventRaw.device_id == device_id)
    )
    prev_time: datetime | None = None
    if last_seq is not None:
        prev_time = session.scalar(
            select(EventRaw.event_time)
            .where(EventRaw.device_id == device_id, EventRaw.seq_no == last_seq)
            .limit(1)
        )
    gap_found = False
    for ev in sorted(events, key=lambda e: e["seq_no"]):
        seq = ev["seq_no"]
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
    result = IngestResult()
    normalized: list[dict[str, Any]] = []
    seen_batch_ids: set[str] = set()

    for raw in raw_events:
        ev = normalize_event(raw)
        if ev is None:
            continue  # malformed: neither accepted nor duplicate
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
        payload = {k: v for k, v in ev.items() if k not in ("event_time",)}
        payload["event_time"] = ev["event_time"].isoformat() + "Z"
        session.add(EventRaw(
            event_id=str(ev["event_id"]),
            tenant_id=str(ev.get("tenant_id") or tenant_id),
            store_id=ev["store_id"],
            device_id=str(ev["device_id"]),
            seq_no=ev["seq_no"],
            type=str(ev["type"]),
            event_time=ev["event_time"],
            payload_json=payload,
        ))
        session.flush()
        _project(session, ev)
        result.accepted += 1

    session.commit()
    return result
