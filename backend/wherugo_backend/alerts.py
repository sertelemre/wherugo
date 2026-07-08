"""Queue alerts + webhook delivery (CONTRACTS section 11).

Rising-edge detection with hysteresis: an alert opens only when a
queue_measurement crosses the threshold FROM BELOW. The "previous state" is
derived from the latest stored queue_sample of the same zone (DB state, never
in-memory), so detection stays correct across multiple workers/processes and
across restarts. While the queue stays above the threshold no second alert is
opened; only after it drops below can a new crossing alert again.

Webhook delivery (store.webhook_url) runs in the background (BackgroundTasks
in the ingest endpoint), stdlib urllib, 5 s timeout. Failures are swallowed:
the alert row is kept with delivered=False.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .metrics import QUEUE_ALERT_LEN, queue_alert
from .models import Alert, QueueSample, Store

WEBHOOK_TIMEOUT_SEC = 5.0


def maybe_open_alert(session: Session, store_id: int, zone_id: int, ts: datetime,
                     queue_len: int, est_wait_sec: float | None) -> dict[str, Any] | None:
    """Called from ingest BEFORE the new queue_sample row is inserted.

    Returns a webhook job dict ({alert_id, url, payload}; url may be None when
    the store has no webhook_url) when a new alert was opened, else None.
    The Alert row is added to the session and flushed (id assigned); it is
    committed together with the rest of the batch."""
    if not queue_alert(queue_len, est_wait_sec):
        return None
    prev = session.execute(
        select(QueueSample.queue_len, QueueSample.est_wait_sec)
        .where(QueueSample.store_id == store_id,
               QueueSample.zone_id == zone_id,
               QueueSample.ts < ts)
        .order_by(QueueSample.ts.desc())
        .limit(1)
    ).first()
    if prev is not None and queue_alert(prev[0], prev[1]):
        return None  # still above threshold: hysteresis, no re-alert
    alert_type = "queue_length" if queue_len >= QUEUE_ALERT_LEN else "queue_wait"
    alert = Alert(
        store_id=store_id,
        zone_id=zone_id,
        type=alert_type,
        ts=ts,
        payload_json={"queue_len": queue_len, "est_wait_sec": est_wait_sec},
        delivered=False,
    )
    session.add(alert)
    session.flush()  # assign alert.id for the webhook job
    store = session.get(Store, store_id)
    payload = {
        "type": alert_type,
        "store_id": store_id,
        "zone_id": zone_id,
        "ts": ts.isoformat() + "Z",
        "queue_len": queue_len,
        "est_wait_sec": est_wait_sec,
    }
    return {"alert_id": alert.id,
            "url": getattr(store, "webhook_url", None),
            "payload": payload}


def _post(url: str, body: bytes, timeout: float) -> bool:
    """Raw HTTP POST (stdlib). Tests monkeypatch THIS function — never hit the
    real network from the test suite."""
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec: operator-supplied URL
        status = getattr(resp, "status", 200)
        return 200 <= int(status) < 300


def deliver_webhook(session_factory, alert_id: int, url: str, payload: dict[str, Any],
                    timeout: float = WEBHOOK_TIMEOUT_SEC) -> bool:
    """Best-effort webhook delivery. Any failure is swallowed: the alert row
    stays with delivered=False (CONTRACTS section 11 — failure never deletes
    the alert). On 2xx the alert is marked delivered=True in a fresh session."""
    try:
        ok = _post(url, json.dumps(payload).encode("utf-8"), timeout)
    except Exception:
        ok = False
    if not ok:
        return False
    try:
        with session_factory() as session:
            alert = session.get(Alert, alert_id)
            if alert is not None:
                alert.delivered = True
                session.commit()
    except Exception:
        pass
    return True
