"""Export rollup CSV (CONTRACTS section 12): hourly zone rollup + k-suppression."""
from __future__ import annotations

import csv
import io
from datetime import timedelta

from helpers import AUTH, ingest, queue_measurement, utcnow, window, zone_enter, zone_exit

HEADER = ["hour", "zone_id", "zone_name", "visits", "unique_visitors",
          "dwell_p50", "dwell_p95", "queue_max", "queue_abandons"]


def _hour_base():
    # A stable full-hour bucket safely in the past.
    return (utcnow() - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)


def _seed_rollup_data(client):
    t0 = _hour_base()
    events = []
    seq = 0
    # zone 2: 12 unique visitors (>= k=10) with 30 s dwells -> dwell published
    for track in range(100, 112):
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=seq), zone_id=2, track_id=track))
        seq += 1
        events.append(zone_exit(seq, t0 + timedelta(seconds=seq + 30), zone_id=2,
                                track_id=track, dwell_sec=30.0))
    # zone 3: only 3 unique visitors (< k) -> dwell fields must be blank
    for track in (300, 301, 302):
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=seq), zone_id=3, track_id=track))
        seq += 1
        events.append(zone_exit(seq, t0 + timedelta(seconds=seq + 45), zone_id=3,
                                track_id=track, dwell_sec=45.0))
    # zone 7 (queue): two samples -> queue_max=4, queue_abandons=2*abandons(=0)+...
    seq += 1
    events.append(queue_measurement(seq, t0 + timedelta(minutes=5), 7, 2, 60.0))
    seq += 1
    events.append(queue_measurement(seq, t0 + timedelta(minutes=10), 7, 4, 120.0))
    ingest(client, events)
    return t0


def _fetch_rows(client, t0):
    resp = client.get("/v1/stores/1/export/rollup", headers=AUTH,
                      params={**window(t0 - timedelta(minutes=5),
                                       t0 + timedelta(hours=1)), "format": "csv"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert ".csv" in resp.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == HEADER
    return rows[1:]


def test_rollup_csv_content_and_k_suppression(client):
    t0 = _seed_rollup_data(client)
    rows = _fetch_rows(client, t0)
    by_zone = {int(r[1]): r for r in rows}

    z2 = by_zone[2]
    assert z2[0] == t0.isoformat() + "Z"  # hour bucket
    assert z2[2] == "Kadın Üst Giyim"
    assert int(z2[3]) == 12 and int(z2[4]) == 12
    assert float(z2[5]) == 30.0 and float(z2[6]) == 30.0  # dwell p50/p95 published
    assert z2[7] == "" and z2[8] == ""  # no queue data for a shelf zone

    z3 = by_zone[3]
    assert int(z3[3]) == 3 and int(z3[4]) == 3
    assert z3[5] == "" and z3[6] == ""  # k<10 -> dwell alanları boş

    z7 = by_zone[7]
    assert int(z7[7]) == 4  # queue_max
    assert z7[8] != ""  # queue_abandons aggregated (0 from helper events)


def test_rollup_rejects_unknown_format(client):
    resp = client.get("/v1/stores/1/export/rollup", headers=AUTH,
                      params={"format": "xlsx"})
    assert resp.status_code == 422


def test_rollup_empty_window_returns_header_only(client):
    t0 = utcnow() - timedelta(days=30)
    resp = client.get("/v1/stores/1/export/rollup", headers=AUTH,
                      params=window(t0, t0 + timedelta(hours=1)))
    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows == [HEADER]


def test_rollup_requires_auth_and_store_ownership(client):
    assert client.get("/v1/stores/1/export/rollup").status_code == 401
    assert client.get("/v1/stores/999/export/rollup", headers=AUTH).status_code == 404
