from datetime import date, datetime, timedelta

import pytest

from wherugo_backend import insights, metrics
from wherugo_backend.models import PosDaily, TrackPosition, ZoneVisit

from helpers import AUTH, ingest, iso, queue_measurement, track_update, utcnow, window, zone_enter, zone_exit


@pytest.fixture()
def db(client):
    """Direct SQLAlchemy session onto the client's seeded in-memory DB."""
    session = client.app.state.sessionmaker()
    yield session
    session.close()


def _visit(db, zone_id: int, track_id: int, enter_ts: datetime,
           dwell_sec: float | None = None) -> None:
    db.add(ZoneVisit(store_id=1, zone_id=zone_id, track_id=track_id,
                     enter_ts=enter_ts,
                     exit_ts=enter_ts + timedelta(seconds=dwell_sec or 0.0),
                     dwell_sec=dwell_sec, classification="dwell", is_staff=False))


def test_footfall_excludes_staff(client):
    t0 = utcnow() - timedelta(minutes=30)
    seq = 0
    events = []
    # staff track 900: flagged staff via track_update, then enters entrance (zone 1)
    seq += 1
    events.append(track_update(seq, t0, track_id=900, x=10.0, y=1.0, is_staff=True))
    seq += 1
    events.append(zone_enter(seq, t0 + timedelta(seconds=1), zone_id=1, track_id=900))
    seq += 1
    events.append(zone_exit(seq, t0 + timedelta(seconds=4), zone_id=1, track_id=900, dwell_sec=3.0))
    # 3 customer tracks
    for i, track in enumerate([101, 102, 103]):
        seq += 1
        events.append(track_update(seq, t0 + timedelta(seconds=10 + i), track_id=track,
                                   x=10.0, y=1.0, is_staff=False))
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=11 + i), zone_id=1, track_id=track))
        seq += 1
        events.append(zone_exit(seq, t0 + timedelta(seconds=14 + i), zone_id=1, track_id=track,
                                dwell_sec=3.0))
    ingest(client, events)

    resp = client.get("/v1/stores/1/metrics", headers=AUTH,
                      params={"metric": "footfall", "granularity": "1h",
                              **window(t0 - timedelta(minutes=5), t0 + timedelta(minutes=5))})
    body = resp.json()
    assert body["total"] == 3.0  # staff excluded
    assert body["quality_badge"] in ("green", "yellow", "red")
    assert "quality_detail" in body


def test_heatmap_k_suppression(client):
    t0 = utcnow() - timedelta(minutes=30)
    events = []
    seq = 0
    # 12 unique tracks in cell (0,0) -> visible
    for track in range(1000, 1012):
        seq += 1
        events.append(track_update(seq, t0 + timedelta(seconds=seq), track_id=track, x=0.2, y=0.2))
    # 9 unique tracks in cell (5,5) -> suppressed (k<10)
    for track in range(2000, 2009):
        seq += 1
        events.append(track_update(seq, t0 + timedelta(seconds=seq), track_id=track, x=5.5, y=5.5))
    ingest(client, events)

    resp = client.get("/v1/stores/1/heatmap", headers=AUTH,
                      params={"cell_m": 1.0, "kind": "density",
                              **window(t0 - timedelta(minutes=5), t0 + timedelta(minutes=5))})
    body = resp.json()
    coords = {(c["x"], c["y"]) for c in body["cells"]}
    assert (0.0, 0.0) in coords
    assert (5.0, 5.0) not in coords  # 9 tracks -> suppressed
    assert body["k_suppressed"] == 1
    visible = next(c for c in body["cells"] if (c["x"], c["y"]) == (0.0, 0.0))
    assert visible["value"] == 12.0


def test_funnel_with_pos_import(client):
    t0 = utcnow() - timedelta(minutes=30)
    seq = 0
    events = []
    # two customers enter the store (entrance zone 1)
    for track in (11, 12):
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=seq), zone_id=1, track_id=track))
        seq += 1
        events.append(zone_exit(seq, t0 + timedelta(seconds=seq + 2), zone_id=1, track_id=track,
                                dwell_sec=2.0))
    # one of them visits a shelf and interacts
    seq += 1
    events.append(zone_enter(seq, t0 + timedelta(seconds=60), zone_id=2, track_id=11))
    seq += 1
    events.append(zone_exit(seq, t0 + timedelta(seconds=90), zone_id=2, track_id=11,
                            dwell_sec=30.0, classification="dwell"))
    from helpers import make_event
    seq += 1
    events.append(make_event("interaction_detected", seq, t0 + timedelta(seconds=75),
                             zone_id=2, track_id=11, interaction="interaction_candidate",
                             duration_sec=8.0))
    ingest(client, events)

    today = utcnow().date().isoformat()
    csv_body = f"date,transactions,revenue\n{today},5,1234.50\n"
    resp = client.post("/v1/stores/1/pos-import", headers=AUTH,
                       files={"file": ("pos.csv", csv_body.encode(), "text/csv")})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"imported": 1}

    # purchased only counts POS of FULLY covered store-local days -> the
    # window must span today's whole local day.
    resp = client.get("/v1/stores/1/funnel", headers=AUTH,
                      params=window(utcnow() - timedelta(days=2), utcnow() + timedelta(days=2)))
    steps = {s["name"]: s["value"] for s in resp.json()["steps"]}
    assert steps["entered"] == 2.0
    assert steps["visited_zone"] == 1.0
    assert steps["interacted"] == 1.0
    assert steps["purchased"] == 5.0


def test_pos_import_upsert(client):
    today = utcnow().date().isoformat()
    for tx in (5, 7):
        csv_body = f"date,transactions,revenue\n{today},{tx},100.0\n"
        resp = client.post("/v1/stores/1/pos-import", headers=AUTH,
                           files={"file": ("pos.csv", csv_body.encode(), "text/csv")})
        assert resp.status_code == 200
    resp = client.get("/v1/stores/1/funnel", headers=AUTH,
                      params=window(utcnow() - timedelta(days=2), utcnow() + timedelta(days=2)))
    steps = {s["name"]: s["value"] for s in resp.json()["steps"]}
    assert steps["purchased"] == 7.0  # upserted, not summed


def test_queue_alert_thresholds(client):
    now = utcnow()
    qzone = 7  # seeded queue zone

    # queue_len >= 5 -> alert
    ingest(client, [queue_measurement(1, now - timedelta(minutes=10), qzone, queue_len=6, est_wait_sec=200.0)])
    body = client.get(f"/v1/stores/1/queues/{qzone}/live", headers=AUTH).json()
    assert body["alert"] is True
    assert body["latest"]["queue_len"] == 6

    # newer calm sample -> no alert
    ingest(client, [queue_measurement(2, now - timedelta(minutes=5), qzone, queue_len=2, est_wait_sec=100.0)])
    body = client.get(f"/v1/stores/1/queues/{qzone}/live", headers=AUTH).json()
    assert body["alert"] is False
    assert len(body["series"]) == 2  # both samples within last hour

    # est_wait_sec >= 300 -> alert even with short queue
    ingest(client, [queue_measurement(3, now - timedelta(minutes=1), qzone, queue_len=2, est_wait_sec=400.0)])
    body = client.get(f"/v1/stores/1/queues/{qzone}/live", headers=AUTH).json()
    assert body["alert"] is True


def test_dwell_stats_and_draw_rate(client):
    t0 = utcnow() - timedelta(minutes=30)
    seq = 0
    events = []
    # 20 customers enter the store; 10 of them (>= k=10) visit shelf zone 2
    for track in range(21, 41):
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=seq), zone_id=1, track_id=track))
        seq += 1
        events.append(zone_exit(seq, t0 + timedelta(seconds=seq + 1), zone_id=1, track_id=track,
                                dwell_sec=1.0))
    for i, track in enumerate(range(21, 31)):
        dwell = 10.0 * (i + 1)  # 10, 20, ..., 100
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=100 + seq), zone_id=2, track_id=track))
        seq += 1
        events.append(zone_exit(seq, t0 + timedelta(seconds=100 + seq + int(dwell)), zone_id=2,
                                track_id=track, dwell_sec=dwell, classification="dwell"))
    ingest(client, events)

    resp = client.get("/v1/stores/1/zones/2/dwell", headers=AUTH,
                      params={"stat": "p50,p95",
                              **window(t0 - timedelta(minutes=5), t0 + timedelta(minutes=10))})
    body = resp.json()
    assert body["visits"] == 10
    assert body["stats"]["p50"] == 55.0  # median of 10..100
    assert body["draw_rate"] == 0.5  # 10 of 20 store visitors
    assert body["quality_badge"] in ("green", "yellow", "red")


def test_dwell_suppressed_below_k(db):
    # 9 unique visitors < k=10 -> stats/draw_rate hidden, visit count stays
    t = datetime(2026, 7, 6, 9, 0)
    for i, track in enumerate(range(900, 909)):
        _visit(db, 2, track, t + timedelta(minutes=i), 60.0 + i)
    db.commit()
    res = metrics.dwell_stats(db, 1, 2, t - timedelta(hours=1), t + timedelta(hours=1))
    assert res["suppressed"] is True
    assert res["stats"]["p50"] is None
    assert res["stats"]["p95"] is None
    assert res["draw_rate"] is None
    assert res["visits"] == 9  # visits count events, not people -> may stay


def test_dwell_not_suppressed_at_k(db):
    t = datetime(2026, 7, 6, 9, 0)
    for i, track in enumerate(range(920, 930)):  # exactly k=10 unique visitors
        _visit(db, 2, track, t + timedelta(minutes=i), 10.0 * (i + 1))
    db.commit()
    res = metrics.dwell_stats(db, 1, 2, t - timedelta(hours=1), t + timedelta(hours=1))
    assert res["suppressed"] is False
    assert res["stats"]["p50"] == 55.0
    assert res["draw_rate"] == 1.0


def test_insights_bundle_skips_suppressed_zones(db):
    t = datetime(2026, 7, 6, 10, 0)
    for i, track in enumerate(range(950, 953)):  # zone 2: 3 visitors -> suppressed
        _visit(db, 2, track, t + timedelta(minutes=i), 45.0)
    for i, track in enumerate(range(960, 972)):  # zone 3: 12 visitors -> visible
        _visit(db, 3, track, t + timedelta(minutes=i), 30.0 + i)
    db.commit()
    bundle = insights.build_metrics_bundle(db, 1, t - timedelta(hours=1), t + timedelta(hours=1))
    names = [z["name"] for z in bundle["zones"]]
    assert "Erkek Üst Giyim" in names       # zone 3
    assert "Kadın Üst Giyim" not in names   # zone 2 suppressed (k<10)


def test_transitions_k_suppression(client):
    t0 = utcnow() - timedelta(minutes=30)
    seq = 0
    events = []
    # 11 tracks: entrance -> shelf 2  (visible); 3 tracks: entrance -> shelf 3 (suppressed)
    for track in range(500, 511):
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=seq * 2), zone_id=1, track_id=track))
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=seq * 2 + 1), zone_id=2, track_id=track))
    for track in range(600, 603):
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=seq * 2), zone_id=1, track_id=track))
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=seq * 2 + 1), zone_id=3, track_id=track))
    ingest(client, events)

    resp = client.get("/v1/stores/1/paths/transitions", headers=AUTH,
                      params=window(t0 - timedelta(minutes=5), t0 + timedelta(minutes=30)))
    body = resp.json()
    pairs = {(m["from_zone_id"], m["to_zone_id"]): m["count"] for m in body["matrix"]}
    assert pairs.get((1, 2)) == 11
    assert (1, 3) not in pairs
    assert body["k_suppressed"] == 1


def test_first_destination(client):
    t0 = utcnow() - timedelta(minutes=30)
    seq = 0
    events = []
    # 12 tracks enter then go first to shelf zone 4
    for track in range(700, 712):
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=seq * 2), zone_id=1, track_id=track))
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=seq * 2 + 1), zone_id=4, track_id=track))
    ingest(client, events)

    resp = client.get("/v1/stores/1/paths/first-destination", headers=AUTH,
                      params=window(t0 - timedelta(minutes=5), t0 + timedelta(minutes=30)))
    body = resp.json()
    assert body["total_tracks"] == 12
    assert body["distribution"][0]["zone_id"] == 4
    assert body["distribution"][0]["count"] == 12


# --- footfall: one count per person per window --------------------------------

def test_footfall_counts_track_once_across_bucket_boundary(db):
    # arrival 10:59 + departure pass through the entrance 11:30 -> ONE person;
    # counted in the bucket of the FIRST entrance visit, so 1h and 1d agree.
    _visit(db, 1, 42, datetime(2026, 7, 6, 10, 59), 30.0)
    _visit(db, 1, 42, datetime(2026, 7, 6, 11, 30), 20.0)
    db.commit()
    t_from, t_to = datetime(2026, 7, 6, 10, 0), datetime(2026, 7, 6, 12, 0)
    ff_1h = metrics.footfall(db, 1, t_from, t_to, "1h")
    ff_1d = metrics.footfall(db, 1, t_from, t_to, "1d")
    assert ff_1h["total"] == 1.0
    assert ff_1h["total"] == ff_1d["total"]
    by_ts = {p["ts"]: p["value"] for p in ff_1h["series"]}
    assert by_ts["2026-07-06T10:00:00Z"] == 1.0
    assert by_ts["2026-07-06T11:00:00Z"] == 0.0


def test_footfall_daily_bucket_uses_store_timezone(db):
    # 22:30Z = 01:30 Europe/Istanbul (already Jul 7 locally) -> the visit lands
    # in the local Jul 7 day bucket, whose UTC start is Jul 6 21:00Z.
    _visit(db, 1, 43, datetime(2026, 7, 6, 22, 30), 10.0)
    db.commit()
    ff = metrics.footfall(db, 1, datetime(2026, 7, 6, 21, 0), datetime(2026, 7, 7, 21, 0), "1d")
    assert ff["total"] == 1.0
    assert ff["series"][0]["ts"] == "2026-07-06T21:00:00Z"
    assert ff["series"][0]["value"] == 1.0


# --- transitions: suppression by unique people ---------------------------------

def test_transitions_single_person_fully_suppressed(db):
    # one person pacing 21 times between two shelves -> 10+10 transition
    # events, but ONE individual: nothing may surface (CONTRACTS k<10)
    t = datetime(2026, 7, 6, 10, 0)
    for i in range(21):
        _visit(db, 2 if i % 2 == 0 else 3, 301, t + timedelta(minutes=i), 30.0)
    db.commit()
    res = metrics.transitions(db, 1, t - timedelta(hours=1), t + timedelta(hours=1))
    assert res["matrix"] == []
    assert res["k_suppressed"] == 2  # both directions suppressed


def test_transitions_display_count_is_events_suppression_is_people(db):
    # 10 unique tracks each walking shelf2 -> shelf3 twice: pair (2,3) has 20
    # events over 10 people -> kept, and the displayed count stays the event count
    t = datetime(2026, 7, 6, 10, 0)
    for track in range(400, 410):
        for rep in range(2):
            base = t + timedelta(minutes=(track - 400) * 10 + rep * 4)
            _visit(db, 2, track, base, 10.0)
            _visit(db, 3, track, base + timedelta(minutes=1), 10.0)
    db.commit()
    res = metrics.transitions(db, 1, t - timedelta(hours=1), t + timedelta(hours=3))
    pairs = {(m["from_zone_id"], m["to_zone_id"]): m["count"] for m in res["matrix"]}
    assert pairs[(2, 3)] == 20
    assert pairs[(3, 2)] == 10
    assert res["k_suppressed"] == 0
    assert all("_k" not in m for m in res["matrix"])


# --- conversion: full store-local days only -------------------------------------

def test_conversion_full_local_day(db):
    for track in range(800, 810):
        _visit(db, 1, track, datetime(2026, 7, 7, 10, 0), 5.0)
    db.add(PosDaily(store_id=1, date=date(2026, 7, 7), transactions=5, revenue=100.0))
    db.commit()
    # [Jul 6 21:00Z, Jul 7 21:00Z] == exactly local Jul 7 in Europe/Istanbul
    res = metrics.conversion(db, 1, datetime(2026, 7, 6, 21, 0), datetime(2026, 7, 7, 21, 0))
    assert res["granularity"] == "1d"
    assert len(res["series"]) == 1
    assert res["series"][0]["ts"] == "2026-07-06T21:00:00Z"
    assert res["series"][0]["value"] == 0.5
    assert res["total"] == 0.5
    assert res["has_data"] is True


def test_conversion_partial_window_has_no_rate(db):
    for track in range(820, 830):
        _visit(db, 1, track, datetime(2026, 7, 7, 10, 0), 5.0)
    db.add(PosDaily(store_id=1, date=date(2026, 7, 7), transactions=500, revenue=1.0))
    db.commit()
    # 1h window: whole-day POS may not be divided by one hour of footfall
    res = metrics.conversion(db, 1, datetime(2026, 7, 7, 10, 0), datetime(2026, 7, 7, 11, 0))
    assert res["series"] == []
    assert res["total"] is None
    assert res["has_data"] is False


def test_conversion_drops_partial_day_from_series(db):
    for track in range(840, 850):
        _visit(db, 1, track, datetime(2026, 7, 7, 10, 0), 5.0)
    for track in range(860, 862):
        _visit(db, 1, track, datetime(2026, 7, 8, 5, 0), 5.0)
    db.add(PosDaily(store_id=1, date=date(2026, 7, 7), transactions=5, revenue=1.0))
    db.add(PosDaily(store_id=1, date=date(2026, 7, 8), transactions=100, revenue=1.0))
    db.commit()
    # window fully covers local Jul 7 but only part of local Jul 8
    res = metrics.conversion(db, 1, datetime(2026, 7, 6, 21, 0), datetime(2026, 7, 8, 9, 0))
    assert [p["ts"] for p in res["series"]] == ["2026-07-06T21:00:00Z"]
    assert res["total"] == 0.5  # Jul 8's whole-day POS excluded


# --- funnel: purchased follows the same full-day rule ----------------------------

def test_funnel_purchased_only_for_fully_covered_days(db):
    for track in (30, 31):
        _visit(db, 1, track, datetime(2026, 7, 7, 10, 0), 2.0)
    _visit(db, 2, 30, datetime(2026, 7, 7, 10, 5), 30.0)
    db.add(PosDaily(store_id=1, date=date(2026, 7, 7), transactions=5, revenue=1.0))
    db.commit()
    partial = metrics.funnel(db, 1, datetime(2026, 7, 7, 9, 0), datetime(2026, 7, 7, 11, 0))
    steps = {s["name"]: s["value"] for s in partial["steps"]}
    assert steps["entered"] == 2.0
    assert steps["purchased"] == 0.0  # 2h window does not cover the POS day
    full = metrics.funnel(db, 1, datetime(2026, 7, 6, 21, 0), datetime(2026, 7, 7, 21, 0))
    steps = {s["name"]: s["value"] for s in full["steps"]}
    assert steps["purchased"] == 5.0


# --- occupancy: peak concurrent visitors ------------------------------------------

def test_occupancy_reports_peak_concurrent(db):
    def pos(track, ts):
        db.add(TrackPosition(store_id=1, camera_id=1, track_id=track, ts=ts,
                             x_m=1.0, y_m=1.0, sigma_cm=20.0, is_staff=False, conf=0.9))

    # 3 people at 10:05, a different 2 at 10:20 -> 5 uniques in the hour but
    # never more than 3 at the same time
    for track in (1, 2, 3):
        pos(track, datetime(2026, 7, 6, 10, 5))
    for track in (4, 5):
        pos(track, datetime(2026, 7, 6, 10, 20))
    db.commit()
    res = metrics.occupancy(db, 1, datetime(2026, 7, 6, 10, 0), datetime(2026, 7, 6, 11, 0), "1h")
    assert res["series"][0]["ts"] == "2026-07-06T10:00:00Z"
    assert res["series"][0]["value"] == 3.0  # peak concurrent, not 5
    assert res["total"] == 3.0
    res_1d = metrics.occupancy(db, 1, datetime(2026, 7, 6, 10, 0), datetime(2026, 7, 6, 11, 0), "1d")
    assert res_1d["series"][0]["ts"] == "2026-07-05T21:00:00Z"  # store-local day bucket
    assert res_1d["series"][0]["value"] == 3.0
