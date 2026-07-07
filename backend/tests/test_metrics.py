from datetime import timedelta

from helpers import AUTH, ingest, iso, queue_measurement, track_update, utcnow, window, zone_enter, zone_exit


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

    resp = client.get("/v1/stores/1/funnel", headers=AUTH,
                      params=window(t0 - timedelta(minutes=5), utcnow() + timedelta(minutes=5)))
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
                      params=window(utcnow() - timedelta(hours=1), utcnow()))
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
    # 4 customers enter the store; 2 of them visit shelf zone 2
    for track in (21, 22, 23, 24):
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=seq), zone_id=1, track_id=track))
        seq += 1
        events.append(zone_exit(seq, t0 + timedelta(seconds=seq + 1), zone_id=1, track_id=track,
                                dwell_sec=1.0))
    for track, dwell in ((21, 10.0), (22, 30.0)):
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
    assert body["visits"] == 2
    assert body["stats"]["p50"] == 20.0  # median of 10 and 30
    assert body["draw_rate"] == 0.5  # 2 of 4 store visitors
    assert body["quality_badge"] in ("green", "yellow", "red")


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
