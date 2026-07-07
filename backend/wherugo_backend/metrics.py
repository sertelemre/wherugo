"""Metric computations. All aggregate outputs respect k<10 suppression where
individual behavior could leak (heatmap, paths); staff tracks are excluded from
customer metrics."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CoverageGap, EventRaw, PosDaily, QueueSample, TrackPosition, Zone, ZoneVisit
from .quality import K_ANONYMITY, k_suppress

QUEUE_ALERT_LEN = 5
QUEUE_ALERT_WAIT_SEC = 300.0


# --- helpers -----------------------------------------------------------------

def _bucket_floor(ts: datetime, granularity: str) -> datetime:
    if granularity == "1d":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    return ts.replace(minute=0, second=0, microsecond=0)


def _bucket_step(granularity: str) -> timedelta:
    return timedelta(days=1) if granularity == "1d" else timedelta(hours=1)


def _buckets(t_from: datetime, t_to: datetime, granularity: str) -> list[datetime]:
    step = _bucket_step(granularity)
    cur = _bucket_floor(t_from, granularity)
    out: list[datetime] = []
    while cur <= t_to and len(out) < 5000:
        out.append(cur)
        cur = cur + step
    return out


def _series(bucket_values: dict[datetime, float], t_from: datetime, t_to: datetime,
            granularity: str) -> list[dict[str, Any]]:
    return [{"ts": b.isoformat() + "Z", "value": bucket_values.get(b, 0.0)}
            for b in _buckets(t_from, t_to, granularity)]


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def _entrance_zone_ids(session: Session, store_id: int) -> list[int]:
    return list(session.scalars(
        select(Zone.id).where(Zone.store_id == store_id, Zone.zone_type == "entrance")))


def _all_zone_visitors(session: Session, store_id: int, t_from: datetime, t_to: datetime) -> set[int]:
    """Unique non-staff track_ids with any zone_visit in window."""
    rows = session.scalars(
        select(ZoneVisit.track_id)
        .where(ZoneVisit.store_id == store_id,
               ZoneVisit.is_staff == False,  # noqa: E712
               ZoneVisit.enter_ts >= t_from,
               ZoneVisit.enter_ts <= t_to)
    ).all()
    return set(rows)


def _entrance_visitors(session: Session, store_id: int, t_from: datetime, t_to: datetime) -> set[int]:
    """Unique non-staff track_ids with an entrance zone_visit in window."""
    zids = _entrance_zone_ids(session, store_id)
    if not zids:
        return set()
    rows = session.scalars(
        select(ZoneVisit.track_id)
        .where(ZoneVisit.store_id == store_id,
               ZoneVisit.zone_id.in_(zids),
               ZoneVisit.is_staff == False,  # noqa: E712
               ZoneVisit.enter_ts >= t_from,
               ZoneVisit.enter_ts <= t_to)
    ).all()
    return set(rows)


# --- footfall / occupancy / conversion ----------------------------------------

def footfall(session: Session, store_id: int, t_from: datetime, t_to: datetime,
             granularity: str = "1h") -> dict[str, Any]:
    """Entrance zone_visit count: is_staff=False, unique track per bucket."""
    zids = _entrance_zone_ids(session, store_id)
    per_bucket: dict[datetime, set[int]] = {}
    if zids:
        rows = session.execute(
            select(ZoneVisit.track_id, ZoneVisit.enter_ts)
            .where(ZoneVisit.store_id == store_id,
                   ZoneVisit.zone_id.in_(zids),
                   ZoneVisit.is_staff == False,  # noqa: E712
                   ZoneVisit.enter_ts >= t_from,
                   ZoneVisit.enter_ts <= t_to)
        ).all()
        for track_id, enter_ts in rows:
            per_bucket.setdefault(_bucket_floor(enter_ts, granularity), set()).add(track_id)
    values = {b: float(len(tracks)) for b, tracks in per_bucket.items()}
    series = _series(values, t_from, t_to, granularity)
    return {"series": series, "total": sum(v for v in values.values()),
            "has_data": bool(per_bucket)}


def occupancy(session: Session, store_id: int, t_from: datetime, t_to: datetime,
              granularity: str = "1h") -> dict[str, Any]:
    """Active (distinct, non-staff) track count per time bucket from track_position."""
    rows = session.execute(
        select(TrackPosition.track_id, TrackPosition.ts)
        .where(TrackPosition.store_id == store_id,
               TrackPosition.is_staff == False,  # noqa: E712
               TrackPosition.ts >= t_from,
               TrackPosition.ts <= t_to)
    ).all()
    per_bucket: dict[datetime, set[int]] = {}
    for track_id, ts in rows:
        per_bucket.setdefault(_bucket_floor(ts, granularity), set()).add(track_id)
    values = {b: float(len(tracks)) for b, tracks in per_bucket.items()}
    series = _series(values, t_from, t_to, granularity)
    peak = max(values.values(), default=0.0)
    return {"series": series, "total": peak, "has_data": bool(rows)}


def conversion(session: Session, store_id: int, t_from: datetime, t_to: datetime,
               granularity: str = "1d") -> dict[str, Any]:
    """POS transactions / footfall per day (conversion is inherently daily)."""
    ff = footfall(session, store_id, t_from, t_to, "1d")
    ff_by_day = {p["ts"][:10]: p["value"] for p in ff["series"]}
    pos_rows = session.execute(
        select(PosDaily.date, PosDaily.transactions)
        .where(PosDaily.store_id == store_id,
               PosDaily.date >= t_from.date(),
               PosDaily.date <= t_to.date())
    ).all()
    tx_by_day = {d.isoformat(): tx for d, tx in pos_rows}
    series = []
    for p in ff["series"]:
        day = p["ts"][:10]
        f = ff_by_day.get(day, 0.0)
        tx = tx_by_day.get(day, 0)
        series.append({"ts": p["ts"], "value": round(tx / f, 4) if f > 0 else 0.0})
    total_ff = sum(ff_by_day.values())
    total_tx = sum(tx_by_day.values())
    overall = round(total_tx / total_ff, 4) if total_ff > 0 else 0.0
    return {"series": series, "total": overall,
            "has_data": bool(pos_rows) or ff["has_data"]}


# --- dwell / draw rate ---------------------------------------------------------

def dwell_stats(session: Session, store_id: int, zone_id: int,
                t_from: datetime, t_to: datetime,
                stats: list[str] | None = None) -> dict[str, Any]:
    stats = stats or ["p50", "p95"]
    visits = session.scalars(
        select(ZoneVisit)
        .where(ZoneVisit.store_id == store_id,
               ZoneVisit.zone_id == zone_id,
               ZoneVisit.is_staff == False,  # noqa: E712
               ZoneVisit.enter_ts >= t_from,
               ZoneVisit.enter_ts <= t_to)
    ).all()
    dwell_values = [v.dwell_sec for v in visits if v.dwell_sec is not None]
    out_stats: dict[str, float | None] = {}
    for s in stats:
        s = s.strip().lower()
        if s.startswith("p") and s[1:].isdigit():
            out_stats[s] = percentile(dwell_values, float(s[1:]))
        elif s in ("avg", "mean"):
            out_stats[s] = (sum(dwell_values) / len(dwell_values)) if dwell_values else None
    zone_visitors = {v.track_id for v in visits}
    store_visitors = _entrance_visitors(session, store_id, t_from, t_to) | _all_zone_visitors(
        session, store_id, t_from, t_to)
    draw_rate = (round(min(1.0, len(zone_visitors) / len(store_visitors)), 4)
                 if store_visitors else None)
    return {"stats": out_stats, "visits": len(visits), "draw_rate": draw_rate,
            "has_data": bool(visits)}


# --- heatmap ---------------------------------------------------------------------

def heatmap(session: Session, store_id: int, t_from: datetime, t_to: datetime,
            cell_m: float = 0.5, kind: str = "density") -> dict[str, Any]:
    """track_position -> cell grid. Cells with <K unique tracks are suppressed.

    density: unique track count per cell; dwell: total seconds (~1 Hz samples)."""
    rows = session.execute(
        select(TrackPosition.track_id, TrackPosition.x_m, TrackPosition.y_m)
        .where(TrackPosition.store_id == store_id,
               TrackPosition.is_staff == False,  # noqa: E712
               TrackPosition.ts >= t_from,
               TrackPosition.ts <= t_to)
    ).all()
    cells: dict[tuple[int, int], dict[str, Any]] = {}
    for track_id, x_m, y_m in rows:
        key = (int(x_m // cell_m), int(y_m // cell_m))
        cell = cells.setdefault(key, {"tracks": set(), "samples": 0})
        cell["tracks"].add(track_id)
        cell["samples"] += 1
    items = [
        {"x": round(ix * cell_m, 3), "y": round(iy * cell_m, 3),
         "value": float(len(c["tracks"])) if kind == "density" else float(c["samples"]),
         "_k": len(c["tracks"])}
        for (ix, iy), c in cells.items()
    ]
    kept, suppressed = k_suppress(items, lambda it: it["_k"], K_ANONYMITY)
    for it in kept:
        it.pop("_k", None)
    kept.sort(key=lambda it: (it["y"], it["x"]))
    return {"cells": kept, "k_suppressed": suppressed, "has_data": bool(rows)}


# --- paths -------------------------------------------------------------------------

def first_destination(session: Session, store_id: int, t_from: datetime, t_to: datetime) -> dict[str, Any]:
    """Distribution of first non-entrance zone visited after entering the store."""
    entrance_ids = set(_entrance_zone_ids(session, store_id))
    visits = session.execute(
        select(ZoneVisit.track_id, ZoneVisit.zone_id, ZoneVisit.enter_ts)
        .where(ZoneVisit.store_id == store_id,
               ZoneVisit.is_staff == False,  # noqa: E712
               ZoneVisit.enter_ts >= t_from,
               ZoneVisit.enter_ts <= t_to)
        .order_by(ZoneVisit.enter_ts)
    ).all()
    first_entry: dict[int, datetime] = {}
    first_dest: dict[int, int] = {}
    for track_id, zone_id, enter_ts in visits:
        if zone_id in entrance_ids:
            first_entry.setdefault(track_id, enter_ts)
        elif track_id in first_entry and track_id not in first_dest and enter_ts >= first_entry[track_id]:
            first_dest[track_id] = zone_id
    counts: dict[int, int] = {}
    for zone_id in first_dest.values():
        counts[zone_id] = counts.get(zone_id, 0) + 1
    zone_names = dict(session.execute(select(Zone.id, Zone.name).where(Zone.store_id == store_id)).all())
    items = [{"zone_id": zid, "zone_name": zone_names.get(zid, str(zid)), "count": n}
             for zid, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    kept, suppressed = k_suppress(items, lambda it: it["count"], K_ANONYMITY)
    return {"distribution": kept, "total_tracks": len(first_dest),
            "k_suppressed": suppressed, "has_data": bool(visits)}


def transitions(session: Session, store_id: int, t_from: datetime, t_to: datetime) -> dict[str, Any]:
    """zone -> zone transition matrix from consecutive visits per track (k<10 suppressed)."""
    visits = session.execute(
        select(ZoneVisit.track_id, ZoneVisit.zone_id, ZoneVisit.enter_ts)
        .where(ZoneVisit.store_id == store_id,
               ZoneVisit.is_staff == False,  # noqa: E712
               ZoneVisit.enter_ts >= t_from,
               ZoneVisit.enter_ts <= t_to)
        .order_by(ZoneVisit.track_id, ZoneVisit.enter_ts)
    ).all()
    counts: dict[tuple[int, int], int] = {}
    prev_by_track: dict[int, int] = {}
    for track_id, zone_id, _enter_ts in visits:
        prev = prev_by_track.get(track_id)
        if prev is not None and prev != zone_id:
            counts[(prev, zone_id)] = counts.get((prev, zone_id), 0) + 1
        prev_by_track[track_id] = zone_id
    zone_names = dict(session.execute(select(Zone.id, Zone.name).where(Zone.store_id == store_id)).all())
    items = [{"from_zone_id": a, "from_zone": zone_names.get(a, str(a)),
              "to_zone_id": b, "to_zone": zone_names.get(b, str(b)), "count": n}
             for (a, b), n in sorted(counts.items(), key=lambda kv: -kv[1])]
    kept, suppressed = k_suppress(items, lambda it: it["count"], K_ANONYMITY)
    return {"matrix": kept, "k_suppressed": suppressed, "has_data": bool(visits)}


# --- queue ---------------------------------------------------------------------------

def queue_alert(queue_len: int | None, est_wait_sec: float | None) -> bool:
    return (queue_len is not None and queue_len >= QUEUE_ALERT_LEN) or \
           (est_wait_sec is not None and est_wait_sec >= QUEUE_ALERT_WAIT_SEC)


def queue_live(session: Session, store_id: int, zone_id: int, now: datetime) -> dict[str, Any]:
    """Latest measurement + last-hour series + alert flag."""
    latest = session.scalars(
        select(QueueSample)
        .where(QueueSample.store_id == store_id, QueueSample.zone_id == zone_id)
        .order_by(QueueSample.ts.desc())
        .limit(1)
    ).first()
    series_rows = session.scalars(
        select(QueueSample)
        .where(QueueSample.store_id == store_id,
               QueueSample.zone_id == zone_id,
               QueueSample.ts >= now - timedelta(hours=1))
        .order_by(QueueSample.ts)
    ).all()

    def to_out(s: QueueSample) -> dict[str, Any]:
        return {"ts": s.ts.isoformat() + "Z", "queue_len": s.queue_len,
                "est_wait_sec": s.est_wait_sec, "joins": s.joins,
                "abandons": s.abandons, "active_checkouts": s.active_checkouts}

    return {
        "latest": to_out(latest) if latest else None,
        "series": [to_out(s) for s in series_rows],
        "alert": queue_alert(latest.queue_len, latest.est_wait_sec) if latest else False,
        "has_data": latest is not None,
    }


# --- funnel / coverage gaps ---------------------------------------------------------

def funnel(session: Session, store_id: int, t_from: datetime, t_to: datetime) -> dict[str, Any]:
    """entered -> visited a zone -> interacted -> purchased (pos_daily)."""
    entrance_visitors = _entrance_visitors(session, store_id, t_from, t_to)
    entrance_ids = set(_entrance_zone_ids(session, store_id))
    visited_rows = session.execute(
        select(ZoneVisit.track_id, ZoneVisit.zone_id)
        .where(ZoneVisit.store_id == store_id,
               ZoneVisit.is_staff == False,  # noqa: E712
               ZoneVisit.enter_ts >= t_from,
               ZoneVisit.enter_ts <= t_to)
    ).all()
    visited = {t for t, z in visited_rows if z not in entrance_ids}
    interactions = session.execute(
        select(EventRaw.payload_json)
        .where(EventRaw.store_id == store_id,
               EventRaw.type == "interaction_detected",
               EventRaw.event_time >= t_from,
               EventRaw.event_time <= t_to)
    ).all()
    interacted = {p[0].get("track_id") for p in interactions if p[0] and p[0].get("track_id") is not None}
    # Monotonluk: zone ziyareti olan herkes magazaya girmistir (giris kamerasi
    # kacirsa bile); etkilesim de ancak bir zone ziyareti icinde olur.
    entered = entrance_visitors | visited
    interacted &= visited
    purchased = session.execute(
        select(PosDaily.transactions)
        .where(PosDaily.store_id == store_id,
               PosDaily.date >= t_from.date(),
               PosDaily.date <= t_to.date())
    ).all()
    tx_total = sum(r[0] for r in purchased)
    has_data = bool(entrance_visitors or visited_rows or interactions or purchased)
    return {
        "steps": [
            {"name": "entered", "value": float(len(entered))},
            {"name": "visited_zone", "value": float(len(visited))},
            {"name": "interacted", "value": float(len(interacted))},
            {"name": "purchased", "value": float(tx_total)},
        ],
        "has_data": has_data,
    }


def coverage_gaps(session: Session, store_id: int, t_from: datetime, t_to: datetime) -> dict[str, Any]:
    rows = session.scalars(
        select(CoverageGap)
        .where(CoverageGap.store_id == store_id)
        .where((CoverageGap.gap_start == None) | (CoverageGap.gap_start <= t_to))  # noqa: E711
        .where((CoverageGap.gap_end == None) | (CoverageGap.gap_end >= t_from))  # noqa: E711
        .order_by(CoverageGap.gap_start)
    ).all()
    gaps = []
    total_sec = 0.0
    for g in rows:
        dur = 0.0
        if g.gap_start and g.gap_end and g.gap_end > g.gap_start:
            dur = (g.gap_end - g.gap_start).total_seconds()
        total_sec += dur
        gaps.append({
            "id": g.id, "device_id": g.device_id,
            "gap_start": g.gap_start.isoformat() + "Z" if g.gap_start else None,
            "gap_end": g.gap_end.isoformat() + "Z" if g.gap_end else None,
            "missing_seq_from": g.missing_seq_from, "missing_seq_to": g.missing_seq_to,
            "reason": g.reason, "duration_sec": round(dur, 3),
        })
    return {"gaps": gaps, "total_minutes": round(total_sec / 60.0, 2)}
