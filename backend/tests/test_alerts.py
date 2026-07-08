"""Alerts + webhook (CONTRACTS section 11): rising-edge hysteresis, delivery."""
from __future__ import annotations

import json
from datetime import timedelta

from wherugo_backend import alerts as alerts_mod

from helpers import AUTH, ingest, queue_measurement, utcnow, window

QUEUE_ZONE = 7  # "Kasa Kuyruğu" in the demo seed


def _alerts(client, t_from, t_to):
    resp = client.get("/v1/stores/1/alerts", headers=AUTH, params=window(t_from, t_to))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_alert_opens_only_on_rising_edge_with_hysteresis(client):
    t0 = utcnow() - timedelta(minutes=30)
    seq = 0
    events = []
    # below -> above -> still above -> below -> above again = exactly 2 alerts
    for minute, qlen, wait in [(0, 2, 60.0), (1, 6, 120.0), (2, 7, 150.0),
                               (3, 1, 20.0), (4, 8, 200.0)]:
        seq += 1
        events.append(queue_measurement(seq, t0 + timedelta(minutes=minute),
                                        QUEUE_ZONE, qlen, wait))
    ingest(client, events)

    alerts = _alerts(client, t0 - timedelta(minutes=1), t0 + timedelta(minutes=10))
    assert len(alerts) == 2
    assert [a["type"] for a in alerts] == ["queue_length", "queue_length"]
    assert alerts[0]["zone_id"] == QUEUE_ZONE
    assert alerts[0]["queue_len"] == 6
    assert alerts[1]["queue_len"] == 8
    assert alerts[0]["delivered"] is False  # no webhook_url configured


def test_alert_hysteresis_survives_separate_batches(client):
    """State is derived from the DB (last queue_sample), not process memory:
    a second batch/worker seeing the queue still above threshold must NOT
    open a second alert."""
    t0 = utcnow() - timedelta(minutes=30)
    ingest(client, [queue_measurement(1, t0, QUEUE_ZONE, 6, 100.0)])
    ingest(client, [queue_measurement(2, t0 + timedelta(minutes=1), QUEUE_ZONE, 9, 250.0)])
    alerts = _alerts(client, t0 - timedelta(minutes=1), t0 + timedelta(minutes=5))
    assert len(alerts) == 1


def test_alert_type_queue_wait(client):
    t0 = utcnow() - timedelta(minutes=30)
    # queue_len below 5 but est_wait_sec >= 300 -> queue_wait
    ingest(client, [queue_measurement(1, t0, QUEUE_ZONE, 2, 400.0)])
    alerts = _alerts(client, t0 - timedelta(minutes=1), t0 + timedelta(minutes=5))
    assert len(alerts) == 1
    assert alerts[0]["type"] == "queue_wait"
    assert alerts[0]["est_wait_sec"] == 400.0


def test_below_threshold_never_alerts(client):
    t0 = utcnow() - timedelta(minutes=30)
    ingest(client, [queue_measurement(1, t0, QUEUE_ZONE, 4, 299.0),
                    queue_measurement(2, t0 + timedelta(minutes=1), QUEUE_ZONE, 3, 100.0)])
    assert _alerts(client, t0 - timedelta(minutes=1), t0 + timedelta(minutes=5)) == []


# --- webhook delivery (never touches the real network: _post is monkeypatched) ---


def _arm_webhook(client, url="http://hook.local/alerts"):
    resp = client.put("/v1/stores/1", headers=AUTH, json={"webhook_url": url})
    assert resp.status_code == 200, resp.text


def test_webhook_posted_and_marked_delivered(client, monkeypatch):
    calls = []

    def fake_post(url, body, timeout):
        calls.append((url, json.loads(body.decode("utf-8")), timeout))
        return True

    monkeypatch.setattr(alerts_mod, "_post", fake_post)
    _arm_webhook(client)
    t0 = utcnow() - timedelta(minutes=10)
    ingest(client, [queue_measurement(1, t0, QUEUE_ZONE, 6, 200.0)])

    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url == "http://hook.local/alerts"
    assert timeout == 5.0  # CONTRACTS section 11: 5 s timeout
    assert payload["type"] == "queue_length"
    assert payload["store_id"] == 1
    assert payload["zone_id"] == QUEUE_ZONE
    assert payload["queue_len"] == 6
    assert payload["est_wait_sec"] == 200.0
    assert payload["ts"].endswith("Z")

    alerts = _alerts(client, t0 - timedelta(minutes=1), t0 + timedelta(minutes=5))
    assert alerts[0]["delivered"] is True


def test_webhook_failure_keeps_alert_undelivered(client, monkeypatch):
    def failing_post(url, body, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(alerts_mod, "_post", failing_post)
    _arm_webhook(client)
    t0 = utcnow() - timedelta(minutes=10)
    # ingest must still succeed (failure is swallowed)
    result = ingest(client, [queue_measurement(1, t0, QUEUE_ZONE, 6, 200.0)])
    assert result["accepted"] == 1

    alerts = _alerts(client, t0 - timedelta(minutes=1), t0 + timedelta(minutes=5))
    assert len(alerts) == 1  # başarısızlık alert'i silmez
    assert alerts[0]["delivered"] is False


def test_no_webhook_call_without_url(client, monkeypatch):
    calls = []
    monkeypatch.setattr(alerts_mod, "_post",
                        lambda url, body, timeout: calls.append(url) or True)
    t0 = utcnow() - timedelta(minutes=10)
    ingest(client, [queue_measurement(1, t0, QUEUE_ZONE, 6, 200.0)])
    assert calls == []
    # alert itself is still recorded
    assert len(_alerts(client, t0 - timedelta(minutes=1), t0 + timedelta(minutes=5))) == 1


def test_duplicate_batch_does_not_duplicate_alert_or_webhook(client, monkeypatch):
    calls = []
    monkeypatch.setattr(alerts_mod, "_post",
                        lambda url, body, timeout: calls.append(url) or True)
    _arm_webhook(client)
    t0 = utcnow() - timedelta(minutes=10)
    ev = queue_measurement(1, t0, QUEUE_ZONE, 6, 200.0)
    ingest(client, [ev])
    result = ingest(client, [ev])  # idempotent retry of the same event
    assert result["duplicates"] == 1
    assert len(calls) == 1
    assert len(_alerts(client, t0 - timedelta(minutes=1), t0 + timedelta(minutes=5))) == 1
