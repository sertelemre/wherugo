"""Shared test helpers: event builders per CONTRACTS section 2 envelope."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

AUTH = {"Authorization": "Bearer demo"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


def make_event(etype: str, seq: int, event_time: datetime, *,
               device_id: str = "edge-1a", store_id: int = 1,
               event_id: str | None = None, **fields) -> dict:
    ev = {
        "tenant_id": "t_demo",
        "store_id": store_id,
        "device_id": device_id,
        "schema_version": 3,
        "seq_no": seq,
        "event_id": event_id or str(uuid.uuid4()),
        "event_time": iso(event_time),
        "ingest_time": None,
        "type": etype,
        "quality": {"conf": 0.9, "coverage_ok": True},
    }
    ev.update(fields)
    return ev


def track_update(seq: int, ts: datetime, track_id: int, x: float, y: float,
                 is_staff: bool = False, **kw) -> dict:
    return make_event("track_update", seq, ts, track_id=track_id,
                      camera_id=1, pos={"x_m": x, "y_m": y, "sigma_cm": 20.0},
                      is_staff=is_staff, **kw)


def zone_enter(seq: int, ts: datetime, zone_id: int, track_id: int, **kw) -> dict:
    return make_event("zone_enter", seq, ts, zone_id=zone_id, track_id=track_id, **kw)


def zone_exit(seq: int, ts: datetime, zone_id: int, track_id: int,
              dwell_sec: float | None = None, classification: str | None = None, **kw) -> dict:
    ev = make_event("zone_exit", seq, ts, zone_id=zone_id, track_id=track_id, **kw)
    if dwell_sec is not None:
        ev["dwell_sec"] = dwell_sec
    if classification is not None:
        ev["classification"] = classification
    return ev


def queue_measurement(seq: int, ts: datetime, zone_id: int, queue_len: int,
                      est_wait_sec: float, **kw) -> dict:
    return make_event("queue_measurement", seq, ts, zone_id=zone_id,
                      queue_len=queue_len, est_wait_sec=est_wait_sec,
                      joins_since_last=1, abandons_since_last=0,
                      active_checkouts=2, **kw)


def ingest(client, events: list[dict]):
    resp = client.post("/v1/ingest/events", json={"events": events}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()


def window(t_from: datetime, t_to: datetime) -> dict:
    return {"from": iso(t_from), "to": iso(t_to)}
