"""Metric computations. All aggregate outputs respect k<10 suppression where
individual behavior could leak (heatmap, paths, transitions, dwell); staff
tracks are excluded from customer metrics.

Timestamps are stored as naive UTC; "1d" buckets are floored to the store's
local calendar day (store.timezone, default Europe/Istanbul) and reported as
the UTC instant of that local midnight."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CoverageGap, EventRaw, PosDaily, QueueSample, Store, TrackPosition, Zone, ZoneVisit
from .quality import K_ANONYMITY, k_suppress

QUEUE_ALERT_LEN = 5
QUEUE_ALERT_WAIT_SEC = 300.0
DEFAULT_TIMEZONE = "Europe/Istanbul"


# --- helpers -----------------------------------------------------------------

def _store_tz(session: Session, store_id: int) -> ZoneInfo:
    store = session.get(Store, store_id)
    name = (store.timezone if store is not None and store.timezone else DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def _to_local(ts: datetime, tz: ZoneInfo) -> datetime:
    """Naive-UTC timestamp -> aware store-local timestamp."""
    return ts.replace(tzinfo=timezone.utc).astimezone(tz)


def _to_utc_naive(local: datetime) -> datetime:
    """Aware local timestamp -> naive-UTC timestamp (storage convention)."""
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _local_day_start(ts: datetime, tz: ZoneInfo) -> datetime:
    """Naive-UTC instant of the store-local midnight containing ts."""
    return _to_utc_naive(_to_local(ts, tz).replace(hour=0, minute=0, second=0, microsecond=0))


def _bucket_floor(ts: datetime, granularity: str, tz: ZoneInfo | None = None) -> datetime:
    if granularity == "1d":
        if tz is not None:
            return _local_day_start(ts, tz)
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    return ts.replace(minute=0, second=0, microsecond=0)


def _bucket_step(granularity: str) -> timedelta:
    return timedelta(days=1) if granularity == "1d" else timedelta(hours=1)


def _buckets(t_from: datetime, t_to: datetime, granularity: str,
             tz: ZoneInfo | None = None) -> list[datetime]:
    out: list[datetime] = []
    if granularity == "1d" and tz is not None:
        # Iterate in local wall-clock so DST days keep their local midnight.
        cur_local = _to_local(t_from, tz).replace(hour=0, minute=0, second=0, microsecond=0)
        while len(out) < 5000:
            cur = _to_utc_naive(cur_local)
            if cur > t_to:
                break
            out.append(cur)
            cur_local = cur_local + timedelta(days=1)
        return out
    step = _bucket_step(granularity)
    cur = _bucket_floor(t_from, granularity)
    while cur <= t_to and len(out) < 5000:
        out.append(cur)
        cur = cur + step
    return out


def _series(bucket_values: dict[datetime, float], t_from: datetime, t_to: datetime,
            granularity: str, tz: ZoneInfo | None = None) -> list[dict[str, Any]]:
    return [{"ts": b.isoformat() + "Z", "value": bucket_values.get(b, 0.0)}
            for b in _buckets(t_from, t_to, granularity, tz)]


def _full_local_days(t_from: datetime, t_to: datetime,
                     tz: ZoneInfo) -> list[tuple[datetime, date]]:
    """Store-local calendar days FULLY covered by [t_from, t_to].

    Returns (day_start_utc_naive, local_date) pairs; partial days at either
    window edge are excluded."""
    out: list[tuple[datetime, date]] = []
    cur_local = _to_local(t_from, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if _to_utc_naive(cur_local) < t_from:
        cur_local = cur_local + timedelta(days=1)
    while len(out) < 5000:
        start = _to_utc_naive(cur_local)
        end = _to_utc_naive(cur_local + timedelta(days=1))
        if end > t_to:
            break
        out.append((start, cur_local.date()))
        cur_local = cur_local + timedelta(days=1)
    return out


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
    """Unique non-staff entrance visitors. Each track is counted ONCE per
    window, in the bucket of its FIRST entrance visit (a visit's departure
    pass through the entrance must not count again), so the total is
    identical across granularities."""
    tz = _store_tz(session, store_id)
    zids = _entrance_zone_ids(session, store_id)
    first_seen: dict[int, datetime] = {}
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
            prev = first_seen.get(track_id)
            if prev is None or enter_ts < prev:
                first_seen[track_id] = enter_ts
    per_bucket: dict[datetime, float] = {}
    for enter_ts in first_seen.values():
        b = _bucket_floor(enter_ts, granularity, tz)
        per_bucket[b] = per_bucket.get(b, 0.0) + 1.0
    series = _series(per_bucket, t_from, t_to, granularity, tz)
    return {"series": series, "total": float(len(first_seen)),
            "has_data": bool(first_seen)}


def occupancy(session: Session, store_id: int, t_from: datetime, t_to: datetime,
              granularity: str = "1h") -> dict[str, Any]:
    """PEAK CONCURRENT (distinct, non-staff) visitors per time bucket: the
    maximum over per-minute distinct track counts within the bucket, from
    ~1 Hz track_position samples. This measures how full the store was at
    its busiest instant of the bucket, not how many people passed through."""
    tz = _store_tz(session, store_id)
    rows = session.execute(
        select(TrackPosition.track_id, TrackPosition.ts)
        .where(TrackPosition.store_id == store_id,
               TrackPosition.is_staff == False,  # noqa: E712
               TrackPosition.ts >= t_from,
               TrackPosition.ts <= t_to)
    ).all()
    minute_tracks: dict[datetime, set[int]] = {}
    for track_id, ts in rows:
        minute_tracks.setdefault(ts.replace(second=0, microsecond=0), set()).add(track_id)
    per_bucket: dict[datetime, float] = {}
    for minute, tracks in minute_tracks.items():
        b = _bucket_floor(minute, granularity, tz)
        per_bucket[b] = max(per_bucket.get(b, 0.0), float(len(tracks)))
    series = _series(per_bucket, t_from, t_to, granularity, tz)
    peak = max(per_bucket.values(), default=0.0)
    return {"series": series, "total": peak, "has_data": bool(rows)}


def conversion(session: Session, store_id: int, t_from: datetime, t_to: datetime,
               granularity: str = "1d") -> dict[str, Any]:
    """POS transactions / footfall per store-local calendar day (conversion is
    inherently daily; the requested granularity is ignored and the response
    carries granularity="1d").

    Only days FULLY covered by [t_from, t_to] are rated: pos_daily holds
    whole-day transaction totals, so dividing them by a partial day's footfall
    would inflate the rate. Partial days are dropped from the series; when the
    window covers no full local day, total=None and has_data=False."""
    tz = _store_tz(session, store_id)
    full_days = _full_local_days(t_from, t_to, tz)
    if not full_days:
        return {"series": [], "total": None, "has_data": False, "granularity": "1d"}
    ff = footfall(session, store_id, t_from, t_to, "1d")
    ff_by_ts = {p["ts"]: p["value"] for p in ff["series"]}
    pos_rows = session.execute(
        select(PosDaily.date, PosDaily.transactions)
        .where(PosDaily.store_id == store_id,
               PosDaily.date.in_([d for _, d in full_days]))
    ).all()
    tx_by_day = {d: tx for d, tx in pos_rows}
    series = []
    total_ff = 0.0
    total_tx = 0
    for day_start, local_date in full_days:
        ts = day_start.isoformat() + "Z"
        f = ff_by_ts.get(ts, 0.0)
        tx = tx_by_day.get(local_date, 0)
        total_ff += f
        total_tx += tx
        series.append({"ts": ts, "value": round(tx / f, 4) if f > 0 else 0.0})
    overall = round(total_tx / total_ff, 4) if total_ff > 0 else 0.0
    return {"series": series, "total": overall,
            "has_data": bool(pos_rows) or total_ff > 0, "granularity": "1d"}


# --- dwell / draw rate ---------------------------------------------------------

def dwell_stats(session: Session, store_id: int, zone_id: int,
                t_from: datetime, t_to: datetime,
                stats: list[str] | None = None) -> dict[str, Any]:
    """Dwell percentiles + visit count + draw rate for a zone.

    k<10 suppression (CONTRACTS section 0): when fewer than K_ANONYMITY unique
    non-staff visitors are behind the aggregate, stats and draw_rate are None
    and suppressed=True — otherwise a single individual's exact dwell time
    would leak. The visits count stays (it counts visits, not people)."""
    stats = stats or ["p50", "p95"]
    visits = session.scalars(
        select(ZoneVisit)
        .where(ZoneVisit.store_id == store_id,
               ZoneVisit.zone_id == zone_id,
               ZoneVisit.is_staff == False,  # noqa: E712
               ZoneVisit.enter_ts >= t_from,
               ZoneVisit.enter_ts <= t_to)
    ).all()
    zone_visitors = {v.track_id for v in visits}
    suppressed = len(zone_visitors) < K_ANONYMITY
    requested = [s.strip().lower() for s in stats]
    out_stats: dict[str, float | None] = {}
    if suppressed:
        for s in requested:
            if (s.startswith("p") and s[1:].isdigit()) or s in ("avg", "mean"):
                out_stats[s] = None
        return {"stats": out_stats, "visits": len(visits), "draw_rate": None,
                "suppressed": True, "has_data": bool(visits)}
    dwell_values = [v.dwell_sec for v in visits if v.dwell_sec is not None]
    for s in requested:
        if s.startswith("p") and s[1:].isdigit():
            out_stats[s] = percentile(dwell_values, float(s[1:]))
        elif s in ("avg", "mean"):
            out_stats[s] = (sum(dwell_values) / len(dwell_values)) if dwell_values else None
    store_visitors = _entrance_visitors(session, store_id, t_from, t_to) | _all_zone_visitors(
        session, store_id, t_from, t_to)
    draw_rate = (round(min(1.0, len(zone_visitors) / len(store_visitors)), 4)
                 if store_visitors else None)
    return {"stats": out_stats, "visits": len(visits), "draw_rate": draw_rate,
            "suppressed": False, "has_data": bool(visits)}


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
    """zone -> zone transition matrix from consecutive visits per track.

    k<10 suppression is applied on the number of UNIQUE tracks behind each
    pair (same pattern as heatmap) — never on the raw event count, so a single
    person pacing between two zones cannot surface their route. The displayed
    count remains the transition-event count."""
    visits = session.execute(
        select(ZoneVisit.track_id, ZoneVisit.zone_id, ZoneVisit.enter_ts)
        .where(ZoneVisit.store_id == store_id,
               ZoneVisit.is_staff == False,  # noqa: E712
               ZoneVisit.enter_ts >= t_from,
               ZoneVisit.enter_ts <= t_to)
        .order_by(ZoneVisit.track_id, ZoneVisit.enter_ts)
    ).all()
    counts: dict[tuple[int, int], int] = {}
    pair_tracks: dict[tuple[int, int], set[int]] = {}
    prev_by_track: dict[int, int] = {}
    for track_id, zone_id, _enter_ts in visits:
        prev = prev_by_track.get(track_id)
        if prev is not None and prev != zone_id:
            key = (prev, zone_id)
            counts[key] = counts.get(key, 0) + 1
            pair_tracks.setdefault(key, set()).add(track_id)
        prev_by_track[track_id] = zone_id
    zone_names = dict(session.execute(select(Zone.id, Zone.name).where(Zone.store_id == store_id)).all())
    items = [{"from_zone_id": a, "from_zone": zone_names.get(a, str(a)),
              "to_zone_id": b, "to_zone": zone_names.get(b, str(b)), "count": n,
              "_k": len(pair_tracks[(a, b)])}
             for (a, b), n in sorted(counts.items(), key=lambda kv: -kv[1])]
    kept, suppressed = k_suppress(items, lambda it: it["_k"], K_ANONYMITY)
    for it in kept:
        it.pop("_k", None)
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
    """entered -> visited a zone -> interacted -> purchased (pos_daily).

    purchased only sums POS transactions of store-local days FULLY covered by
    the window (same rule as conversion): pos_daily is a whole-day total, so a
    partial day would put more purchases than visitors into the funnel."""
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
    full_day_dates = [d for _, d in _full_local_days(t_from, t_to, _store_tz(session, store_id))]
    purchased = []
    if full_day_dates:
        purchased = session.execute(
            select(PosDaily.transactions)
            .where(PosDaily.store_id == store_id,
                   PosDaily.date.in_(full_day_dates))
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
    """Gaps intersecting the window. duration_sec/total_minutes count only the
    portion of each gap that overlaps [t_from, t_to] (same clipping as
    quality.gap_overlap_seconds); raw gap_start/gap_end stay unclipped."""
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
            start = max(g.gap_start, t_from)
            end = min(g.gap_end, t_to)
            if end > start:
                dur = (end - start).total_seconds()
        total_sec += dur
        gaps.append({
            "id": g.id, "device_id": g.device_id,
            "gap_start": g.gap_start.isoformat() + "Z" if g.gap_start else None,
            "gap_end": g.gap_end.isoformat() + "Z" if g.gap_end else None,
            "missing_seq_from": g.missing_seq_from, "missing_seq_to": g.missing_seq_to,
            "reason": g.reason, "duration_sec": round(dur, 3),
        })
    return {"gaps": gaps, "total_minutes": round(total_sec / 60.0, 2)}
