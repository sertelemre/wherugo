"""Publisher: spool'da biriktirme (backend yokken), boşaltma, kalıcı seq_no."""
from datetime import datetime, timezone

import requests

import wherugo_edge.publisher as publisher_mod
from wherugo_edge.config import (
    CameraDef,
    EdgeConfig,
    PublisherParams,
    ZoneDef,
)
from wherugo_edge.events import Quality, TrackUpdate
from wherugo_edge.homography import Homography
from wherugo_edge.publisher import Publisher

TS = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)


def mk_config():
    return EdgeConfig(
        tenant_id="t_demo",
        store_id=1,
        device_id="edge-1a",
        backend_url="http://localhost:8000",
        cameras=[CameraDef(id=1, name="cam", homography=Homography.identity())],
        zones=[ZoneDef(id=1, name="giris", zone_type="entrance", polygon=[(0, 0), (1, 0), (1, 1)])],
        publisher=PublisherParams(batch_size=100),
    )


def mk_event(i):
    return TrackUpdate(
        event_time=TS, camera_id=1, track_id=1000 + i, x_m=1.0, y_m=2.0,
        sigma_cm=20.0, is_staff=False, quality=Quality(conf=0.9),
    )


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {"accepted": 0, "duplicates": 0, "gap_detected": False}

    def json(self):
        return self._body


def test_offline_events_accumulate_in_spool(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise requests.ConnectionError("backend kapalı")

    monkeypatch.setattr(publisher_mod.requests, "post", refuse)
    pub = Publisher(mk_config(), spool_path=tmp_path / "spool.sqlite")
    for i in range(3):
        pub.publish(mk_event(i))
    stats = pub.flush()
    assert stats["sent"] == 0
    assert stats["pending_spool"] == 3
    assert pub.buffer == []
    assert pub.store.count() == 3
    pub.close()


def test_spool_drains_in_seq_order_when_backend_returns(tmp_path, monkeypatch):
    posted = []

    def refuse(*a, **k):
        raise requests.ConnectionError("backend kapalı")

    monkeypatch.setattr(publisher_mod.requests, "post", refuse)
    pub = Publisher(mk_config(), spool_path=tmp_path / "spool.sqlite")
    for i in range(3):
        pub.publish(mk_event(i))
    pub.flush()  # -> spool

    def accept(url, json=None, headers=None, timeout=None):
        posted.append((url, json, headers))
        return FakeResponse(200)

    monkeypatch.setattr(publisher_mod.requests, "post", accept)
    pub.publish(mk_event(3))  # yeni olay bellek kuyruğunda
    stats = pub.flush()
    assert stats["sent"] == 4
    assert pub.store.count() == 0

    all_events = [ev for _, body, _ in posted for ev in body["events"]]
    seqs = [ev["envelope"]["seq_no"] for ev in all_events]
    assert seqs == [1, 2, 3, 4]  # spool önce, sıra korunur
    assert posted[0][0] == "http://localhost:8000/v1/ingest/events"
    assert posted[0][2]["Authorization"] == "Bearer demo"
    pub.close()


def test_seq_no_persists_across_restart(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise requests.ConnectionError("backend kapalı")

    monkeypatch.setattr(publisher_mod.requests, "post", refuse)
    path = tmp_path / "spool.sqlite"

    pub1 = Publisher(mk_config(), spool_path=path)
    for i in range(2):
        pub1.publish(mk_event(i))
    pub1.close()  # flush edilmemiş kuyruk diske iner

    pub2 = Publisher(mk_config(), spool_path=path)
    assert pub2.store.count() == 2  # önceki olaylar spool'da
    wire = pub2.publish(mk_event(2))
    assert wire["envelope"]["seq_no"] == 3  # monotonluk restart sonrası sürer
    pub2.close()


def test_non_200_response_spools(tmp_path, monkeypatch):
    monkeypatch.setattr(
        publisher_mod.requests, "post", lambda *a, **k: FakeResponse(503)
    )
    pub = Publisher(mk_config(), spool_path=tmp_path / "spool.sqlite")
    pub.publish(mk_event(0))
    stats = pub.flush()
    assert stats["sent"] == 0
    assert pub.store.count() == 1
    pub.close()
