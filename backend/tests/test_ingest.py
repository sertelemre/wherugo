from datetime import timedelta

from sqlalchemy import select

from wherugo_backend.models import CoverageGap, EventRaw, Store, Tenant, TrackPosition, ZoneVisit

from helpers import AUTH, ingest, iso, make_event, queue_measurement, track_update, utcnow, window, zone_enter, zone_exit


def test_ingest_idempotency_same_event_id_twice(client):
    t0 = utcnow() - timedelta(minutes=10)
    ev = track_update(1, t0, track_id=100, x=1.0, y=1.0)

    r1 = ingest(client, [ev])
    assert r1 == {"accepted": 1, "duplicates": 0, "gap_detected": False, "rejected": 0}

    r2 = ingest(client, [ev])
    assert r2 == {"accepted": 0, "duplicates": 1, "gap_detected": False, "rejected": 0}


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
    assert r == {"accepted": 5, "duplicates": 0, "gap_detected": False, "rejected": 0}


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


# --- seq_no reset (C5) ---------------------------------------------------------


def test_seq_reset_recorded_and_gap_tracking_resumes(client):
    t0 = utcnow() - timedelta(minutes=30)
    # historic stream up to seq 1000
    ingest(client, [
        track_update(999, t0, track_id=1, x=1.0, y=1.0),
        track_update(1000, t0 + timedelta(seconds=1), track_id=1, x=1.1, y=1.0),
    ])
    # device restarts: counter resets to 1, and 3..49 is a REAL hole
    r = ingest(client, [
        track_update(1, t0 + timedelta(seconds=60), track_id=2, x=1.0, y=1.0),
        track_update(2, t0 + timedelta(seconds=61), track_id=2, x=1.1, y=1.0),
        track_update(50, t0 + timedelta(seconds=120), track_id=2, x=2.0, y=1.0),
    ])
    assert r["gap_detected"] is True
    with client.app.state.sessionmaker() as session:
        gaps = session.scalars(select(CoverageGap).order_by(CoverageGap.id)).all()
        reasons = [g.reason for g in gaps]
        assert "seq_reset" in reasons
        seq_gaps = [g for g in gaps if g.reason == "seq_gap"]
        assert len(seq_gaps) == 1
        assert seq_gaps[0].missing_seq_from == 3
        assert seq_gaps[0].missing_seq_to == 49

    # gap tracking keeps working from the new base (50 -> 60 misses 51..59)
    r2 = ingest(client, [track_update(60, t0 + timedelta(seconds=180), track_id=2, x=2.5, y=1.0)])
    assert r2["gap_detected"] is True
    with client.app.state.sessionmaker() as session:
        seq_gaps = session.scalars(
            select(CoverageGap).where(CoverageGap.reason == "seq_gap").order_by(CoverageGap.id)
        ).all()
        assert (seq_gaps[-1].missing_seq_from, seq_gaps[-1].missing_seq_to) == (51, 59)


# --- concurrent duplicate retry (C8) --------------------------------------------


def test_concurrent_duplicate_ingest_resolves_without_error(tmp_path, monkeypatch):
    """A retry of the same batch committed between our dedup check and our
    commit must resolve to duplicates via IntegrityError retry, not blow up."""
    from wherugo_backend import ingest as ingest_mod
    from wherugo_backend.db import Base, create_db_engine, create_session_factory

    engine = create_db_engine(f"sqlite:///{tmp_path / 'race.db'}")
    Base.metadata.create_all(engine)
    make_session = create_session_factory(engine)
    with make_session() as s:
        s.add(Tenant(id="t_demo", name="T", isolation_tier="shared"))
        s.add(Store(id=1, tenant_id="t_demo", name="S", plan_width_m=10.0,
                    plan_height_m=10.0, timezone="Europe/Istanbul"))
        s.commit()

    ev = track_update(1, utcnow() - timedelta(minutes=1), track_id=7, x=1.0, y=1.0,
                      device_id="edge-race")

    orig_touch = ingest_mod._touch_device
    fired = {"done": False}

    def racing_touch(session, device_id, store_id):
        # First pass only: simulate the concurrent retry committing the same
        # batch after our dedup check already returned "not a duplicate".
        if not fired["done"]:
            fired["done"] = True
            session.rollback()  # release our read txn so the other connection can commit
            with make_session() as other:
                res_b = ingest_mod.process_batch(other, "t_demo", [ev])
                assert res_b.accepted == 1
        orig_touch(session, device_id, store_id)

    monkeypatch.setattr(ingest_mod, "_touch_device", racing_touch)
    with make_session() as sa:
        res = ingest_mod.process_batch(sa, "t_demo", [ev])  # must NOT raise
    assert res.accepted == 0
    assert res.duplicates == 1
    with make_session() as s:
        assert len(s.scalars(select(EventRaw)).all()) == 1
        assert len(s.scalars(select(TrackPosition)).all()) == 1
    engine.dispose()


# --- tenant isolation (C12) ------------------------------------------------------


def _seed_other_tenant_store(client, store_id: int = 2):
    with client.app.state.sessionmaker() as s:
        s.add(Tenant(id="t_other", name="Other", isolation_tier="shared"))
        s.add(Store(id=store_id, tenant_id="t_other", name="Rakip Mağaza",
                    plan_width_m=10.0, plan_height_m=10.0, timezone="Europe/Istanbul"))
        s.commit()


def test_ingest_rejects_foreign_and_unknown_store(client):
    _seed_other_tenant_store(client)
    t0 = utcnow() - timedelta(minutes=10)
    r = ingest(client, [
        track_update(1, t0, track_id=11, x=1.0, y=1.0, store_id=2),    # tenant B's store
        track_update(2, t0, track_id=12, x=1.0, y=1.0, store_id=999),  # unknown store
        track_update(3, t0, track_id=13, x=1.0, y=1.0),                # own store
    ])
    assert r == {"accepted": 1, "duplicates": 0, "gap_detected": False, "rejected": 2}
    with client.app.state.sessionmaker() as session:
        assert session.scalars(select(EventRaw).where(EventRaw.store_id != 1)).all() == []
        assert session.scalars(select(TrackPosition).where(TrackPosition.store_id != 1)).all() == []


def test_ingest_tenant_id_always_from_auth(client):
    t0 = utcnow() - timedelta(minutes=10)
    ev = track_update(1, t0, track_id=21, x=1.0, y=1.0)
    ev["tenant_id"] = "t_evil"  # body claims another tenant; store 1 is ours
    r = ingest(client, [ev])
    assert r["accepted"] == 1
    with client.app.state.sessionmaker() as session:
        row = session.get(EventRaw, ev["event_id"])
        assert row.tenant_id == "t_demo"  # auth tenant, not the body's


# --- malformed events must not drop the batch (C16 + C17) -------------------------


def test_malformed_event_does_not_drop_batch(client):
    t0 = utcnow() - timedelta(minutes=10)
    good1 = track_update(1, t0, track_id=31, x=1.0, y=1.0)
    bad = track_update(2, t0 + timedelta(seconds=1), track_id=31, x=1.0, y=1.0)
    bad["track_id"] = "abc"
    good2 = track_update(3, t0 + timedelta(seconds=2), track_id=31, x=1.2, y=1.0)

    r = ingest(client, [good1, bad, good2])
    assert r["accepted"] == 2
    assert r["rejected"] == 1

    # the good events really persisted: resending them counts duplicates
    r2 = ingest(client, [good1, good2])
    assert r2 == {"accepted": 0, "duplicates": 2, "gap_detected": False, "rejected": 0}


def test_each_malformed_field_rejected_without_500(client):
    t0 = utcnow() - timedelta(minutes=10)
    bad_events = []
    e = track_update(1, t0, track_id=41, x=1.0, y=1.0)
    e["pos"] = {"x_m": "oops", "y_m": 1.0}
    bad_events.append(e)
    bad_events.append(zone_enter(2, t0, zone_id="x", track_id=42))
    bad_events.append(zone_exit(3, t0, zone_id=2, track_id=43, dwell_sec="fast"))
    bad_events.append(queue_measurement(4, t0, zone_id=7, queue_len="many", est_wait_sec=10.0))
    e2 = track_update(5, t0, track_id=44, x=1.0, y=1.0)
    e2["event_time"] = "not-a-date"
    bad_events.append(e2)
    e3 = track_update(6, t0, track_id=45, x=1.0, y=1.0)
    e3["event_time"] = 1700000000  # epoch int is not RFC3339
    bad_events.append(e3)
    e4 = track_update(7, t0, track_id=46, x=1.0, y=1.0)
    e4["quality"] = "corrupted"  # quality must be an object
    bad_events.append(e4)

    for ev in bad_events:
        r = ingest(client, [ev])  # helper asserts HTTP 200
        assert r["accepted"] == 0, ev
        assert r["rejected"] == 1, ev


# --- dwell_sec sanity (m10) ---------------------------------------------------------


def test_negative_and_absurd_dwell_rejected(client):
    t0 = utcnow() - timedelta(minutes=10)
    r = ingest(client, [zone_exit(1, t0, zone_id=4, track_id=51, dwell_sec=-30.0)])
    assert r["accepted"] == 0 and r["rejected"] == 1
    r = ingest(client, [zone_exit(2, t0, zone_id=4, track_id=52, dwell_sec=90000.0)])
    assert r["accepted"] == 0 and r["rejected"] == 1
    with client.app.state.sessionmaker() as session:
        assert session.scalars(select(ZoneVisit)).all() == []

    # paired visit: negative-dwell exit is rejected, the open visit stays open
    ingest(client, [zone_enter(3, t0, zone_id=4, track_id=53)])
    r = ingest(client, [zone_exit(4, t0 + timedelta(seconds=10), zone_id=4, track_id=53,
                                  dwell_sec=-5.0)])
    assert r["rejected"] == 1
    with client.app.state.sessionmaker() as session:
        visits = session.scalars(select(ZoneVisit)).all()
        assert len(visits) == 1
        assert visits[0].exit_ts is None  # untouched by the rejected exit
        assert visits[0].dwell_sec is None
        # no visit may ever have enter_ts in the future or negative dwell
        for v in visits:
            assert v.enter_ts <= utcnow()
