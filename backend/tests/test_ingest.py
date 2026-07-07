from datetime import timedelta

from sqlalchemy import select

from wherugo_backend.models import CoverageGap, ZoneVisit

from helpers import AUTH, ingest, iso, make_event, track_update, utcnow, window, zone_enter, zone_exit


def test_ingest_idempotency_same_event_id_twice(client):
    t0 = utcnow() - timedelta(minutes=10)
    ev = track_update(1, t0, track_id=100, x=1.0, y=1.0)

    r1 = ingest(client, [ev])
    assert r1 == {"accepted": 1, "duplicates": 0, "gap_detected": False}

    r2 = ingest(client, [ev])
    assert r2 == {"accepted": 0, "duplicates": 1, "gap_detected": False}


def test_ingest_duplicate_within_same_batch(client):
    t0 = utcnow() - timedelta(minutes=10)
    ev = track_update(1, t0, track_id=100, x=1.0, y=1.0)
    r = ingest(client, [ev, ev])
    assert r["accepted"] == 1
    assert r["duplicates"] == 1


def test_seq_gap_creates_coverage_gap(client):
    t0 = utcnow() - timedelta(minutes=10)
    r = ingest(client, [
        track_update(1, t0, track_id=100, x=1.0, y=1.0),
        track_update(5, t0 + timedelta(seconds=60), track_id=100, x=2.0, y=1.0),
    ])
    assert r["gap_detected"] is True

    with client.app.state.sessionmaker() as session:
        gaps = session.scalars(select(CoverageGap)).all()
        assert len(gaps) == 1
        gap = gaps[0]
        assert gap.reason == "seq_gap"
        assert gap.missing_seq_from == 2
        assert gap.missing_seq_to == 4
        assert gap.device_id == "edge-1a"

    resp = client.get("/v1/stores/1/coverage-gaps", headers=AUTH,
                      params=window(t0 - timedelta(minutes=5), t0 + timedelta(minutes=5)))
    body = resp.json()
    assert len(body["gaps"]) == 1
    assert body["total_minutes"] == 1.0


def test_contiguous_seq_no_gap(client):
    t0 = utcnow() - timedelta(minutes=10)
    r = ingest(client, [
        track_update(i, t0 + timedelta(seconds=i), track_id=100, x=1.0, y=1.0)
        for i in range(1, 6)
    ])
    assert r == {"accepted": 5, "duplicates": 0, "gap_detected": False}


def test_zone_visit_enter_exit_pairing(client):
    t0 = utcnow() - timedelta(minutes=10)
    ingest(client, [
        zone_enter(1, t0, zone_id=2, track_id=101),
        zone_exit(2, t0 + timedelta(seconds=30), zone_id=2, track_id=101,
                  dwell_sec=30.0, classification="dwell"),
    ])
    with client.app.state.sessionmaker() as session:
        visits = session.scalars(select(ZoneVisit)).all()
        assert len(visits) == 1
        v = visits[0]
        assert v.zone_id == 2 and v.track_id == 101
        assert v.exit_ts is not None
        assert v.dwell_sec == 30.0
        assert v.classification == "dwell"


def test_lone_zone_exit_still_creates_visit(client):
    t0 = utcnow() - timedelta(minutes=10)
    ingest(client, [
        zone_exit(1, t0, zone_id=4, track_id=202, dwell_sec=12.0),
    ])
    with client.app.state.sessionmaker() as session:
        visits = session.scalars(select(ZoneVisit)).all()
        assert len(visits) == 1
        v = visits[0]
        assert v.zone_id == 4 and v.track_id == 202
        assert v.exit_ts is not None and v.dwell_sec == 12.0
        assert (v.exit_ts - v.enter_ts).total_seconds() == 12.0


def test_nested_envelope_form_accepted(client):
    t0 = utcnow() - timedelta(minutes=10)
    ev = {
        "envelope": {"tenant_id": "t_demo", "store_id": 1, "device_id": "edge-1b",
                     "schema_version": 3, "seq_no": 1,
                     "event_id": "11111111-1111-1111-1111-111111111111",
                     "event_time": iso(t0), "ingest_time": None},
        "type": "track_update", "camera_id": 1, "track_id": 300,
        "pos": {"x_m": 3.0, "y_m": 3.0, "sigma_cm": 15.0},
        "quality": {"conf": 0.9, "coverage_ok": True}, "is_staff": False,
    }
    r = ingest(client, [ev])
    assert r["accepted"] == 1


def test_batch_size_limit(client):
    t0 = utcnow()
    events = [track_update(i, t0, track_id=1, x=1.0, y=1.0) for i in range(1, 502)]
    resp = client.post("/v1/ingest/events", json={"events": events}, headers=AUTH)
    assert resp.status_code == 413


def test_ingest_requires_auth(client):
    resp = client.post("/v1/ingest/events", json={"events": []})
    assert resp.status_code == 401
    resp = client.post("/v1/ingest/events", json={"events": []},
                       headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
