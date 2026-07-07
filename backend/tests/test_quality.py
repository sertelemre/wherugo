from datetime import timedelta

from wherugo_backend.quality import badge_from_gap, k_suppress

from helpers import AUTH, ingest, track_update, utcnow, window


def test_badge_rules_pure():
    hour = 3600.0
    assert badge_from_gap(0.0, hour, True) == "green"
    assert badge_from_gap(0.02 * hour, hour, True) == "green"      # exactly 2% -> green
    assert badge_from_gap(0.05 * hour, hour, True) == "yellow"     # >2%
    assert badge_from_gap(0.10 * hour, hour, True) == "yellow"     # exactly 10% -> yellow
    assert badge_from_gap(0.15 * hour, hour, True) == "red"        # >10%
    assert badge_from_gap(0.0, hour, False) == "red"               # no data -> red


def test_k_suppress_helper():
    rows = [{"n": 12}, {"n": 10}, {"n": 9}, {"n": 1}]
    kept, suppressed = k_suppress(rows, lambda r: r["n"])
    assert [r["n"] for r in kept] == [12, 10]
    assert suppressed == 2


def test_endpoint_badge_red_when_no_data(client):
    t0 = utcnow() - timedelta(days=30)
    resp = client.get("/v1/stores/1/metrics", headers=AUTH,
                      params={"metric": "footfall", **window(t0, t0 + timedelta(hours=1))})
    body = resp.json()
    assert body["quality_badge"] == "red"
    assert body["quality_detail"]["has_data"] is False
    assert body["quality_detail"]["reason"] == "no_data"


def test_endpoint_badge_yellow_on_small_gap(client):
    # 120 s gap within a 3600 s window -> 3.3% -> yellow
    t0 = utcnow() - timedelta(hours=2)
    ingest(client, [
        track_update(1, t0, track_id=1, x=1.0, y=1.0),
        track_update(5, t0 + timedelta(seconds=120), track_id=1, x=1.0, y=1.0),
    ])
    resp = client.get("/v1/stores/1/metrics", headers=AUTH,
                      params={"metric": "occupancy", **window(t0, t0 + timedelta(hours=1))})
    body = resp.json()
    assert body["quality_badge"] == "yellow"
    assert body["quality_detail"]["coverage_gap_sec"] == 120.0


def test_endpoint_badge_red_on_large_gap(client):
    # 600 s gap within a 3600 s window -> 16.7% -> red
    t0 = utcnow() - timedelta(hours=2)
    ingest(client, [
        track_update(1, t0, track_id=1, x=1.0, y=1.0),
        track_update(9, t0 + timedelta(seconds=600), track_id=1, x=1.0, y=1.0),
    ])
    resp = client.get("/v1/stores/1/metrics", headers=AUTH,
                      params={"metric": "occupancy", **window(t0, t0 + timedelta(hours=1))})
    assert resp.json()["quality_badge"] == "red"
