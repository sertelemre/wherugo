"""Management API (CONTRACTS section 10): store/zone/device CRUD + tenant isolation."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from wherugo_backend.models import Store, Tenant, Zone, ZoneVisit

from helpers import AUTH, ingest, utcnow, zone_enter, zone_exit

STORE_BODY = {"name": "Yeni Mağaza", "plan_width_m": 15.0, "plan_height_m": 10.0,
              "timezone": "Europe/Istanbul"}


def _seed_foreign_store(client):
    with client.app.state.sessionmaker() as s:
        s.add(Tenant(id="t_other", name="Other", isolation_tier="shared"))
        s.add(Store(id=77, tenant_id="t_other", name="Rakip Mağaza",
                    plan_width_m=10.0, plan_height_m=10.0, timezone="Europe/Istanbul"))
        s.add(Zone(id=771, store_id=77, name="Giriş", zone_type="entrance", polygon_json=None))
        s.commit()


# --- stores ----------------------------------------------------------------------


def test_create_store_and_shape_matches_get(client):
    resp = client.post("/v1/stores", headers=AUTH, json=STORE_BODY)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["tenant_id"] == "t_demo"
    assert created["name"] == "Yeni Mağaza"
    assert created["zones"] == []
    assert created["webhook_url"] is None
    # same shape as GET /v1/stores/{id}
    got = client.get(f"/v1/stores/{created['id']}", headers=AUTH).json()
    assert got == created
    ids = [s["id"] for s in client.get("/v1/stores", headers=AUTH).json()]
    assert created["id"] in ids and 1 in ids


def test_update_store_partial(client):
    resp = client.put("/v1/stores/1", headers=AUTH,
                      json={"name": "Demo v2", "webhook_url": "http://hook.local/x"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Demo v2"
    assert body["webhook_url"] == "http://hook.local/x"
    assert body["plan_width_m"] == 20.0  # untouched fields keep their value
    # clearing webhook_url with explicit null
    body = client.put("/v1/stores/1", headers=AUTH, json={"webhook_url": None}).json()
    assert body["webhook_url"] is None
    assert body["name"] == "Demo v2"


def test_create_store_validation(client):
    assert client.post("/v1/stores", headers=AUTH,
                       json={"name": "", "plan_width_m": 1, "plan_height_m": 1}
                       ).status_code == 422
    assert client.post("/v1/stores", headers=AUTH,
                       json={"name": "X", "plan_width_m": -5, "plan_height_m": 1}
                       ).status_code == 422


# --- zones -----------------------------------------------------------------------


def test_zone_crud(client):
    poly = [[0, 0], [2, 0], [2, 2], [0, 2]]
    resp = client.post("/v1/stores/1/zones", headers=AUTH,
                       json={"name": "Yeni Reyon", "zone_type": "shelf",
                             "polygon": poly, "category": "cocuk"})
    assert resp.status_code == 201, resp.text
    zone = resp.json()
    zid = zone["id"]
    assert zone["polygon"] == poly and zone["category"] == "cocuk"

    resp = client.put(f"/v1/stores/1/zones/{zid}", headers=AUTH,
                      json={"name": "Çocuk Reyonu"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Çocuk Reyonu"
    assert resp.json()["zone_type"] == "shelf"  # partial update keeps the rest

    zone_ids = [z["id"] for z in client.get("/v1/stores/1", headers=AUTH).json()["zones"]]
    assert zid in zone_ids

    assert client.delete(f"/v1/stores/1/zones/{zid}", headers=AUTH).status_code == 204
    zone_ids = [z["id"] for z in client.get("/v1/stores/1", headers=AUTH).json()["zones"]]
    assert zid not in zone_ids
    # gone means gone: further updates 404
    assert client.put(f"/v1/stores/1/zones/{zid}", headers=AUTH,
                      json={"name": "x"}).status_code == 404


def test_zone_delete_keeps_historical_visits(client):
    resp = client.post("/v1/stores/1/zones", headers=AUTH,
                       json={"name": "Geçici", "zone_type": "shelf",
                             "polygon": [[0, 0], [1, 0], [1, 1]]})
    zid = resp.json()["id"]
    t0 = utcnow() - timedelta(minutes=10)
    ingest(client, [zone_enter(1, t0, zone_id=zid, track_id=42),
                    zone_exit(2, t0 + timedelta(seconds=30), zone_id=zid,
                              track_id=42, dwell_sec=30.0)])
    assert client.delete(f"/v1/stores/1/zones/{zid}", headers=AUTH).status_code == 204
    with client.app.state.sessionmaker() as s:
        n = s.scalar(select(func.count()).select_from(ZoneVisit)
                     .where(ZoneVisit.zone_id == zid))
    assert n == 1  # silinen zone'un geçmiş verisi kalır (CONTRACTS section 10)


def test_zone_invalid_type_422(client):
    assert client.post("/v1/stores/1/zones", headers=AUTH,
                       json={"name": "X", "zone_type": "garaj",
                             "polygon": [[0, 0], [1, 0], [1, 1]]}).status_code == 422


# --- devices ----------------------------------------------------------------------


def test_device_create_and_list(client):
    resp = client.post("/v1/stores/1/devices", headers=AUTH,
                       json={"id": "edge-9z", "name": "Yeni Cihaz"})
    assert resp.status_code == 201, resp.text
    dev = resp.json()
    assert dev == {"id": "edge-9z", "store_id": 1, "name": "Yeni Cihaz",
                   "last_heartbeat": None, "online": False, "health": None}

    ids = [d["id"] for d in client.get("/v1/stores/1/devices", headers=AUTH).json()]
    assert ids == ["edge-1a", "edge-9z"]  # seeded device + new one

    # duplicate id -> 409
    assert client.post("/v1/stores/1/devices", headers=AUTH,
                       json={"id": "edge-9z", "name": "Kopya"}).status_code == 409


# --- tenant isolation ---------------------------------------------------------------


def test_management_endpoints_404_for_foreign_store(client):
    _seed_foreign_store(client)
    assert client.put("/v1/stores/77", headers=AUTH, json={"name": "x"}).status_code == 404
    assert client.post("/v1/stores/77/zones", headers=AUTH,
                       json={"name": "X", "zone_type": "shelf",
                             "polygon": [[0, 0], [1, 0], [1, 1]]}).status_code == 404
    assert client.put("/v1/stores/77/zones/771", headers=AUTH,
                      json={"name": "x"}).status_code == 404
    assert client.delete("/v1/stores/77/zones/771", headers=AUTH).status_code == 404
    assert client.post("/v1/stores/77/devices", headers=AUTH,
                       json={"id": "edge-x"}).status_code == 404
    assert client.get("/v1/stores/77/devices", headers=AUTH).status_code == 404
    assert client.get("/v1/stores/77/alerts", headers=AUTH).status_code == 404
    assert client.get("/v1/stores/77/export/rollup", headers=AUTH).status_code == 404
    # and the foreign zone was NOT touched
    with client.app.state.sessionmaker() as s:
        assert s.get(Zone, 771) is not None


def test_created_store_not_visible_to_other_tenant_zone_ops(client):
    """A zone created via the API belongs to OUR store only; a foreign store's
    zone id cannot be addressed through our store path (cross-store 404)."""
    _seed_foreign_store(client)
    # zone 771 belongs to store 77: addressing it via store 1 must 404
    assert client.put("/v1/stores/1/zones/771", headers=AUTH,
                      json={"name": "x"}).status_code == 404
    assert client.delete("/v1/stores/1/zones/771", headers=AUTH).status_code == 404
