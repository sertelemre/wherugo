from datetime import timedelta

from wherugo_backend.models import Store, Tenant, Zone

from helpers import AUTH, ingest, track_update, utcnow, window, zone_enter, zone_exit


def test_stores_require_auth(client):
    assert client.get("/v1/stores").status_code == 401
    assert client.get("/v1/stores", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_demo_seed_store_and_zones(client):
    resp = client.get("/v1/stores", headers=AUTH)
    assert resp.status_code == 200
    stores = resp.json()
    assert len(stores) == 1
    store = stores[0]
    assert store["id"] == 1
    assert store["name"] == "Demo Mağaza"
    assert store["plan_width_m"] == 20.0
    assert store["plan_height_m"] == 12.0
    zones = store["zones"]
    assert len(zones) == 8
    types = [z["zone_type"] for z in zones]
    assert types.count("entrance") == 1
    assert types.count("shelf") == 4
    assert types.count("fitting_room") == 1
    assert types.count("queue") == 1
    assert types.count("checkout") == 1


def test_store_detail_and_404(client):
    assert client.get("/v1/stores/1", headers=AUTH).status_code == 200
    assert client.get("/v1/stores/999", headers=AUTH).status_code == 404


def test_device_health_after_ingest(client):
    assert client.get("/v1/admin/devices/edge-1a/health", headers=AUTH).status_code == 200
    body = client.get("/v1/admin/devices/edge-1a/health", headers=AUTH).json()
    assert body["online"] is False  # seeded but never heartbeated

    ingest(client, [track_update(1, utcnow() - timedelta(seconds=5), track_id=1, x=1.0, y=1.0)])
    body = client.get("/v1/admin/devices/edge-1a/health", headers=AUTH).json()
    assert body["online"] is True
    assert body["last_heartbeat"] is not None

    assert client.get("/v1/admin/devices/ghost/health", headers=AUTH).status_code == 404


def test_root_serves_info_or_dashboard(client):
    resp = client.get("/")
    assert resp.status_code == 200
    # dashboard/index.html is another agent's deliverable; accept either mode
    ct = resp.headers["content-type"]
    assert "html" in ct or "json" in ct


# --- invalid from/to -> 422, not 500 (C17+C18) ------------------------------------


def test_invalid_window_params_return_422(client):
    for url, params in [
        ("/v1/stores/1/metrics", {"from": "banana"}),
        ("/v1/stores/1/metrics", {"to": "zzz"}),
        ("/v1/stores/1/zones/2/dwell", {"from": "banana"}),
        ("/v1/stores/1/heatmap", {"to": "not-a-time"}),
        ("/v1/stores/1/paths/first-destination", {"from": "banana"}),
        ("/v1/stores/1/paths/transitions", {"from": "banana"}),
        ("/v1/stores/1/coverage-gaps", {"to": "zzz"}),
        ("/v1/stores/1/funnel", {"from": "07/07/2026"}),
    ]:
        resp = client.get(url, headers=AUTH, params=params)
        assert resp.status_code == 422, (url, params, resp.status_code, resp.text)

    # valid + blank values still work
    now = utcnow()
    assert client.get("/v1/stores/1/metrics", headers=AUTH,
                      params=window(now - timedelta(hours=1), now)).status_code == 200
    assert client.get("/v1/stores/1/metrics", headers=AUTH,
                      params={"from": "", "to": ""}).status_code == 200


# --- pos-import robustness (C19) ---------------------------------------------------


def test_pos_import_non_utf8_returns_422(client):
    body = "date,transactions,revenue\n2026-07-07,5,100.0\n".encode("utf-16")
    resp = client.post("/v1/stores/1/pos-import", headers=AUTH,
                       files={"file": ("pos.csv", body, "text/csv")})
    assert resp.status_code == 422
    assert "UTF-8" in resp.json()["detail"]


def test_pos_import_skips_bad_rows_and_counts_them(client):
    csv_body = ("date,transactions,revenue\n"
                "2026-07-07,5,100.0\n"
                "bozuk;satir;;\n"
                "2026-07-08,abc,100\n"
                "2026-07-09,3,50\n")
    resp = client.post("/v1/stores/1/pos-import", headers=AUTH,
                       files={"file": ("pos.csv", csv_body.encode(), "text/csv")})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"imported": 2, "skipped": 2}


def test_pos_import_clean_file_keeps_historic_shape(client):
    csv_body = "date,transactions,revenue\n2026-07-07,5,100.0\n"
    resp = client.post("/v1/stores/1/pos-import", headers=AUTH,
                       files={"file": ("pos.csv", csv_body.encode(), "text/csv")})
    assert resp.status_code == 200
    assert resp.json() == {"imported": 1}  # no "skipped" key when nothing skipped


# --- briefing?date= validation (C20) -------------------------------------------------


def test_briefing_invalid_date_returns_422(client):
    for bad in ("2026-13-99", "07/07/2026", "yarın"):
        resp = client.get("/v1/stores/1/briefing", headers=AUTH, params={"date": bad})
        assert resp.status_code == 422, (bad, resp.status_code)


# --- tenant isolation on /v1/stores/{id}/* (C12) --------------------------------------


def _seed_foreign_store(client):
    with client.app.state.sessionmaker() as s:
        s.add(Tenant(id="t_other", name="Other", isolation_tier="shared"))
        s.add(Store(id=2, tenant_id="t_other", name="Rakip Mağaza",
                    plan_width_m=10.0, plan_height_m=10.0, timezone="Europe/Istanbul"))
        s.add(Zone(id=99, store_id=2, name="Giriş", zone_type="entrance", polygon_json=None))
        s.commit()


def test_store_endpoints_404_for_foreign_tenant_store(client):
    _seed_foreign_store(client)
    get_urls = [
        "/v1/stores/2",
        "/v1/stores/2/metrics",
        "/v1/stores/2/zones/99/dwell",
        "/v1/stores/2/heatmap",
        "/v1/stores/2/paths/first-destination",
        "/v1/stores/2/paths/transitions",
        "/v1/stores/2/queues/99/live",
        "/v1/stores/2/coverage-gaps",
        "/v1/stores/2/funnel",
        "/v1/stores/2/briefing",
    ]
    for url in get_urls:
        assert client.get(url, headers=AUTH).status_code == 404, url
    assert client.post("/v1/stores/2/assistant", headers=AUTH,
                       json={"question": "?"}).status_code == 404
    assert client.post("/v1/stores/2/pos-import", headers=AUTH,
                       files={"file": ("pos.csv", b"date,transactions\n", "text/csv")}
                       ).status_code == 404
    # and the store list must not leak it either
    ids = [s["id"] for s in client.get("/v1/stores", headers=AUTH).json()]
    assert ids == [1]


# --- metrics response adaptations (handover) ------------------------------------------


def test_conversion_partial_window_returns_200_with_null_total(client):
    # a 1-hour window covers no full store-local day: no 500, total=null,
    # and the response is labeled with the metric's effective granularity 1d
    now = utcnow()
    resp = client.get("/v1/stores/1/metrics", headers=AUTH,
                      params={"metric": "conversion", "granularity": "1h",
                              **window(now - timedelta(hours=1), now)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metric"] == "conversion"
    assert body["granularity"] == "1d"  # forced by the metric, not the request
    assert body["total"] is None
    assert body["series"] == []
    assert body["quality_badge"] in ("green", "yellow", "red")


def test_dwell_endpoint_exposes_suppressed_flag(client):
    t0 = utcnow() - timedelta(minutes=30)
    events = []
    seq = 0
    for track in (61, 62, 63):  # 3 unique visitors < k=10 -> suppressed
        seq += 1
        events.append(zone_enter(seq, t0 + timedelta(seconds=seq), zone_id=2, track_id=track))
        seq += 1
        events.append(zone_exit(seq, t0 + timedelta(seconds=seq + 30), zone_id=2,
                                track_id=track, dwell_sec=30.0))
    ingest(client, events)
    resp = client.get("/v1/stores/1/zones/2/dwell", headers=AUTH,
                      params=window(t0 - timedelta(minutes=5), t0 + timedelta(minutes=5)))
    body = resp.json()
    assert body["suppressed"] is True
    assert body["stats"]["p50"] is None
    assert body["visits"] == 3
