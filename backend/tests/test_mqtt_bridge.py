"""MQTT köprüsü birim testleri (CONTRACTS §13).

paho-mqtt import ETMEDEN köprünün mesaj işleme çekirdeğini
(`handle_message`) sahte mesajlarla test eder: modül import'u ve mesaj
işleme yolu paho'suz çalışmak zorundadır.
"""
import json
import sys

from helpers import make_event, queue_measurement, utcnow, zone_enter

from wherugo_backend.mqtt_bridge import handle_message, parse_payload, tenant_from_topic

TOPIC = "t/t_demo/1/edge-1a/events"


def _payload(events: list[dict]) -> bytes:
    return json.dumps({"events": events}).encode("utf-8")


def _session_factory(client):
    # conftest'in client fixture'ı in-memory DB'li app kurar (demo seed dahil);
    # köprü aynı session factory'yi kullanır.
    return client.app.state.sessionmaker


def test_module_import_does_not_require_paho():
    # Köprü modülü paho'suz import edilebilmeli: modül seviyesinde paho
    # import'u YOKTUR (yalnız main() içinde). Bu test dosyası mqtt_bridge'i
    # import etti; paho hâlâ yüklenmemiş olmalı.
    assert "wherugo_backend.mqtt_bridge" in sys.modules
    assert not any(m == "paho" or m.startswith("paho.") for m in sys.modules)


def test_tenant_from_topic():
    assert tenant_from_topic("t/t_demo/1/edge-1a/events") == "t_demo"
    assert tenant_from_topic("t/other/2/dev/events") == "other"
    assert tenant_from_topic("x/t_demo/1/edge-1a/events") is None
    assert tenant_from_topic("t/t_demo/1/edge-1a/status") is None
    assert tenant_from_topic("t/t_demo/1/events") is None
    assert tenant_from_topic("t//1/edge-1a/events") is None


def test_parse_payload():
    assert parse_payload(b'{"events": []}') == []
    assert parse_payload(b'not json') is None
    assert parse_payload(b'[1, 2]') is None
    assert parse_payload(b'{"no_events": 1}') is None
    assert parse_payload(b'{"events": {"a": 1}}') is None


def test_handle_message_ingests_batch(client):
    ts = utcnow()
    events = [
        zone_enter(1, ts, zone_id=2, track_id=101),
        queue_measurement(2, ts, zone_id=7, queue_len=3, est_wait_sec=120.0),
    ]
    result = handle_message(_session_factory(client), TOPIC, _payload(events))
    assert result is not None
    assert result.accepted == 2
    assert result.rejected == 0

    # Projeksiyon backend'in HTTP ingest'iyle aynı: metrik API'si veriyi görür.
    resp = client.get("/v1/stores/1/queues/7/live",
                      headers={"Authorization": "Bearer demo"})
    assert resp.status_code == 200
    assert resp.json()["latest"]["queue_len"] == 3


def test_handle_message_idempotent_duplicates(client):
    ts = utcnow()
    ev = zone_enter(1, ts, zone_id=2, track_id=42, event_id="11111111-1111-4111-8111-111111111111")
    sf = _session_factory(client)
    first = handle_message(sf, TOPIC, _payload([ev]))
    second = handle_message(sf, TOPIC, _payload([ev]))  # at-least-once yeniden teslim
    assert first.accepted == 1
    assert second.accepted == 0
    assert second.duplicates == 1


def test_tenant_comes_from_topic_not_envelope(client):
    # Zarf t_demo dese bile topic başka tenant'ı gösteriyorsa olay o tenant
    # olarak işlenir; store 1 t_demo'ya ait olduğundan reddedilir (izolasyon).
    ts = utcnow()
    ev = make_event("track_update", 1, ts, track_id=1,
                    pos={"x_m": 1.0, "y_m": 1.0})  # zarfta tenant_id=t_demo
    result = handle_message(_session_factory(client),
                            "t/t_evil/1/edge-1a/events", _payload([ev]))
    assert result is not None
    assert result.accepted == 0
    assert result.rejected == 1


def test_malformed_topic_and_payload_skipped(client):
    sf = _session_factory(client)
    # Geçersiz topic: loglanır, atlanır.
    assert handle_message(sf, "garbage/topic", _payload([])) is None
    # Bozuk JSON: loglanır, atlanır.
    assert handle_message(sf, TOPIC, b"\x00\xffnot-json") is None
    # events listesi olmayan gövde: atlanır.
    assert handle_message(sf, TOPIC, b'{"events": 42}') is None


def test_poisoned_event_does_not_drop_batch(client):
    ts = utcnow()
    good = zone_enter(1, ts, zone_id=2, track_id=7)
    bad = {"type": "zone_enter"}  # zarf alanları eksik
    result = handle_message(_session_factory(client), TOPIC, _payload([bad, good]))
    assert result is not None
    assert result.accepted == 1
    assert result.rejected == 1
