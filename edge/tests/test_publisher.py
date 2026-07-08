"""Publisher: spool'a kalıcı yazma (at-least-once), boşaltma, kalıcı seq_no,
spool sınırı ve kalıcı 4xx düşürme."""
import os
import subprocess
import sys
import textwrap
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
from wherugo_edge.publisher import Publisher, SpoolStore

TS = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)


def mk_config(**pub_kwargs):
    params = {"batch_size": 100}
    params.update(pub_kwargs)
    return EdgeConfig(
        tenant_id="t_demo",
        store_id=1,
        device_id="edge-1a",
        backend_url="http://127.0.0.1:9",  # discard portu: gerçek HTTP asla başarılı olmaz
        cameras=[CameraDef(id=1, name="cam", homography=Homography.identity())],
        zones=[ZoneDef(id=1, name="giris", zone_type="entrance", polygon=[(0, 0), (1, 0), (1, 1)])],
        publisher=PublisherParams(**params),
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


def test_publish_is_durable_before_flush(tmp_path, monkeypatch):
    """publish() olayı ANINDA spool'a yazar: flush/close olmadan da diskte."""
    def refuse(*a, **k):
        raise AssertionError("bu testte HTTP çağrısı olmamalı")

    monkeypatch.setattr(publisher_mod.requests, "post", refuse)
    path = tmp_path / "spool.sqlite"
    pub = Publisher(mk_config(), spool_path=path)
    for i in range(3):
        pub.publish(mk_event(i))
    # flush/close YOK: bağımsız bir bağlantı yine de olayları görmeli
    other = SpoolStore(path)
    assert other.count() == 3
    assert other.next_seq() == 4
    seqs = [s for s, _ in other.load(10)]
    assert seqs == [1, 2, 3]  # sayaç ile spool tutarlı: delik yok
    other.close()
    pub.close()


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
    pub.flush()  # backend kapalı: spool'da kalır

    def accept(url, json=None, headers=None, timeout=None):
        posted.append((url, json, headers))
        return FakeResponse(200)

    monkeypatch.setattr(publisher_mod.requests, "post", accept)
    pub.publish(mk_event(3))  # yeni olay da spool'a yazılır
    stats = pub.flush()
    assert stats["sent"] == 4
    assert pub.store.count() == 0

    all_events = [ev for _, body, _ in posted for ev in body["events"]]
    seqs = [ev["envelope"]["seq_no"] for ev in all_events]
    assert seqs == [1, 2, 3, 4]  # seq sırası korunur
    assert posted[0][0] == "http://127.0.0.1:9/v1/ingest/events"
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
    pub1.close()  # olaylar zaten publish anında diskte

    pub2 = Publisher(mk_config(), spool_path=path)
    assert pub2.store.count() == 2  # önceki olaylar spool'da
    wire = pub2.publish(mk_event(2))
    assert wire["envelope"]["seq_no"] == 3  # monotonluk restart sonrası sürer
    pub2.close()


def test_non_200_response_stays_spooled(tmp_path, monkeypatch):
    """Geçici hata (5xx): olay spool'da bekler, düşürülmez."""
    monkeypatch.setattr(
        publisher_mod.requests, "post", lambda *a, **k: FakeResponse(503)
    )
    pub = Publisher(mk_config(), spool_path=tmp_path / "spool.sqlite")
    pub.publish(mk_event(0))
    stats = pub.flush()
    assert stats["sent"] == 0
    assert stats["dropped"] == 0
    assert pub.store.count() == 1
    pub.close()


def test_crash_after_publish_recovers_events(tmp_path, monkeypatch):
    """Crash senaryosu: publish sonrası süreç ölür (os._exit, flush/close yok) →
    yeni Publisher AYNI olayları spool'dan gönderir (at-least-once)."""
    path = tmp_path / "spool.sqlite"
    script = tmp_path / "crash_publisher.py"
    script.write_text(textwrap.dedent("""
        import os, sys
        from datetime import datetime, timezone
        from wherugo_edge.config import CameraDef, EdgeConfig, PublisherParams, ZoneDef
        from wherugo_edge.events import Quality, TrackUpdate
        from wherugo_edge.homography import Homography
        from wherugo_edge.publisher import Publisher

        cfg = EdgeConfig(
            tenant_id="t_demo", store_id=1, device_id="edge-1a",
            backend_url="http://127.0.0.1:9",
            cameras=[CameraDef(id=1, name="cam", homography=Homography.identity())],
            zones=[ZoneDef(id=1, name="giris", zone_type="entrance",
                           polygon=[(0, 0), (1, 0), (1, 1)])],
            publisher=PublisherParams(batch_size=100),
        )
        pub = Publisher(cfg, spool_path=sys.argv[1])
        ts = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(5):
            pub.publish(TrackUpdate(event_time=ts, camera_id=1, track_id=1000 + i,
                                    x_m=1.0, y_m=2.0, sigma_cm=20.0, is_staff=False,
                                    quality=Quality(conf=0.9)))
        os._exit(9)  # kill -9 benzeri ani ölüm: flush/close ÇALIŞMAZ
    """), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script), str(path)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 9, proc.stderr

    posted = []

    def accept(url, json=None, headers=None, timeout=None):
        posted.append(json)
        return FakeResponse(200)

    monkeypatch.setattr(publisher_mod.requests, "post", accept)
    pub = Publisher(mk_config(), spool_path=path)
    assert pub.store.count() == 5  # çökmeden önceki olaylar diskte
    stats = pub.flush()
    assert stats["sent"] == 5
    seqs = [ev["envelope"]["seq_no"] for body in posted for ev in body["events"]]
    assert seqs == [1, 2, 3, 4, 5]  # hiç olay kaybolmadı, delik yok
    wire = pub.publish(mk_event(9))
    assert wire["envelope"]["seq_no"] == 6  # sayaç kaldığı yerden sürer
    pub.close()


def test_spool_cap_evicts_oldest_with_warning(tmp_path, monkeypatch, capsys):
    def refuse(*a, **k):
        raise requests.ConnectionError("backend kapalı")

    monkeypatch.setattr(publisher_mod.requests, "post", refuse)
    pub = Publisher(mk_config(spool_max_events=5), spool_path=tmp_path / "spool.sqlite")
    for i in range(8):
        pub.publish(mk_event(i))
    assert pub.store.count() == 5  # sınır korunur
    seqs = [s for s, _ in pub.store.load(100)]
    assert seqs == [4, 5, 6, 7, 8]  # en eskiler (1-3) silindi
    assert pub.enveloper.next_seq == 9  # sayaç geri sarılmaz: backend gap görür
    err = capsys.readouterr().err
    assert "spool sınırı" in err
    pub.close()


def test_spool_cap_configurable_and_unlimited(tmp_path):
    store = SpoolStore(tmp_path / "s1.sqlite", max_events=0)  # 0 = sınırsız
    wires = [
        {"envelope": {"seq_no": i}, "type": "track_update"} for i in range(1, 301)
    ]
    store.append_many(wires)
    assert store.count() == 300
    store.close()


def test_permanent_4xx_drops_batch_and_logs(tmp_path, monkeypatch, capsys):
    """Kalıcı 4xx (400/413/422): batch spool'dan düşürülür — sonsuz döngü yok."""
    monkeypatch.setattr(
        publisher_mod.requests, "post", lambda *a, **k: FakeResponse(422)
    )
    pub = Publisher(mk_config(), spool_path=tmp_path / "spool.sqlite")
    for i in range(3):
        pub.publish(mk_event(i))
    stats = pub.flush()
    assert stats["sent"] == 0
    assert stats["dropped"] == 3
    assert pub.store.count() == 0  # tekrar denenmeyecek
    err = capsys.readouterr().err
    assert "kalıcı red" in err and "422" in err
    pub.close()


def test_flush_continues_past_permanently_rejected_batch(tmp_path, monkeypatch):
    """İlk batch 422 ile düşer, sonraki batch normal gönderilir."""
    responses = [FakeResponse(422), FakeResponse(200)]
    monkeypatch.setattr(
        publisher_mod.requests, "post", lambda *a, **k: responses.pop(0)
    )
    pub = Publisher(mk_config(batch_size=2), spool_path=tmp_path / "spool.sqlite")
    for i in range(4):
        pub.publish(mk_event(i))
    stats = pub.flush()
    assert stats["dropped"] == 2
    assert stats["sent"] == 2
    assert pub.store.count() == 0
    pub.close()


def test_auth_401_is_treated_as_transient(tmp_path, monkeypatch):
    """401 (yanlış token) kalıcı red DEĞİL: token düzeltilince olaylar gitmeli."""
    monkeypatch.setattr(
        publisher_mod.requests, "post", lambda *a, **k: FakeResponse(401)
    )
    pub = Publisher(mk_config(), spool_path=tmp_path / "spool.sqlite")
    pub.publish(mk_event(0))
    stats = pub.flush()
    assert stats["dropped"] == 0
    assert pub.store.count() == 1
    pub.close()
