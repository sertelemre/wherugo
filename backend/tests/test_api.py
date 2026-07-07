from datetime import timedelta

from helpers import AUTH, ingest, track_update, utcnow


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
