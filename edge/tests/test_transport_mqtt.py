"""MQTT taşıma (CONTRACTS §13): topic/qos/payload, spool'un taşımadan
bağımsız at-least-once davranışı, paho yokken anlaşılır hata.

Testler paho'yu sahte modülle mock'lar — ağa/broker'a asla çıkılmaz.
"""
import json
import sys
import types
from datetime import datetime, timezone

import pytest

from wherugo_edge.config import (
    CameraDef,
    ConfigError,
    EdgeConfig,
    MqttParams,
    PublisherParams,
    ZoneDef,
    config_from_dict,
)
from wherugo_edge.events import Quality, TrackUpdate
from wherugo_edge.homography import Homography
from wherugo_edge.publisher import HttpTransport, MqttTransport, Publisher, make_transport

TS = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)


def mk_config(transport="mqtt", **mqtt_kwargs):
    return EdgeConfig(
        tenant_id="t_demo",
        store_id=1,
        device_id="edge-1a",
        backend_url="http://127.0.0.1:9",
        cameras=[CameraDef(id=1, name="cam", homography=Homography.identity())],
        zones=[ZoneDef(id=1, name="giris", zone_type="entrance", polygon=[(0, 0), (1, 0), (1, 1)])],
        publisher=PublisherParams(
            batch_size=100, transport=transport, mqtt=MqttParams(**mqtt_kwargs)
        ),
    )


def mk_event(i):
    return TrackUpdate(
        event_time=TS, camera_id=1, track_id=1000 + i, x_m=1.0, y_m=2.0,
        sigma_cm=20.0, is_staff=False, quality=Quality(conf=0.9),
    )


BASE_DICT = {
    "tenant_id": "t_demo",
    "store_id": 1,
    "device_id": "edge-1a",
    "backend_url": "http://localhost:8000",
    "cameras": [{"id": 1}],
    "zones": [{"id": 1, "zone_type": "entrance", "polygon": [[0, 0], [1, 0], [1, 1]]}],
}


# -- sahte paho ------------------------------------------------------------


class FakeBroker:
    """Test kontrollü 'broker': down=True iken bağlantı reddedilir."""

    def __init__(self):
        self.down = False
        self.published = []  # (topic, payload, qos)


class FakeMsgInfo:
    def __init__(self, published):
        self._published = published

    def wait_for_publish(self, timeout=None):
        pass

    def is_published(self):
        return self._published


def install_fake_paho(monkeypatch, broker):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.connected = False

        def connect(self, host, port, keepalive=60):
            if broker.down:
                raise ConnectionRefusedError("broker kapalı")
            self.connected = True

        def loop_start(self):
            pass

        def loop_stop(self):
            pass

        def disconnect(self):
            self.connected = False

        def publish(self, topic, payload, qos=0):
            if broker.down:
                return FakeMsgInfo(False)
            broker.published.append((topic, payload, qos))
            return FakeMsgInfo(True)

    client_mod = types.ModuleType("paho.mqtt.client")
    client_mod.Client = FakeClient
    mqtt_pkg = types.ModuleType("paho.mqtt")
    mqtt_pkg.client = client_mod
    paho_pkg = types.ModuleType("paho")
    paho_pkg.mqtt = mqtt_pkg
    monkeypatch.setitem(sys.modules, "paho", paho_pkg)
    monkeypatch.setitem(sys.modules, "paho.mqtt", mqtt_pkg)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", client_mod)
    return broker


# -- config şeması ----------------------------------------------------------


def test_config_defaults_to_http_transport():
    """Vars. http: mevcut davranış birebir korunur, mqtt bloğu opsiyoneldir."""
    cfg = config_from_dict(dict(BASE_DICT))
    assert cfg.publisher.transport == "http"
    assert cfg.publisher.mqtt.host == "localhost"
    assert cfg.publisher.mqtt.port == 1883
    assert cfg.publisher.mqtt.qos == 1
    assert isinstance(make_transport(cfg), HttpTransport)


def test_config_parses_mqtt_block():
    data = dict(BASE_DICT)
    data["publisher"] = {"transport": "mqtt", "mqtt": {"host": "broker.local", "port": 2883, "qos": 2}}
    cfg = config_from_dict(data)
    assert cfg.publisher.transport == "mqtt"
    assert cfg.publisher.mqtt.host == "broker.local"
    assert cfg.publisher.mqtt.port == 2883
    assert cfg.publisher.mqtt.qos == 2


@pytest.mark.parametrize(
    "publisher_block",
    [
        {"transport": "kafka"},
        {"transport": "mqtt", "mqtt": {"qos": 7}},
        {"transport": "mqtt", "mqtt": {"port": 0}},
    ],
)
def test_config_rejects_invalid_transport_settings(publisher_block):
    data = dict(BASE_DICT)
    data["publisher"] = publisher_block
    with pytest.raises(ConfigError):
        config_from_dict(data)


# -- mqtt gönderim ----------------------------------------------------------


def test_mqtt_publishes_batch_to_contract_topic(tmp_path, monkeypatch):
    """Topic t/{tenant}/{store}/{device}/events, qos 1, payload aynı JSON batch."""
    broker = install_fake_paho(monkeypatch, FakeBroker())
    pub = Publisher(mk_config(), spool_path=tmp_path / "spool.sqlite")
    assert isinstance(pub.transport, MqttTransport)
    for i in range(3):
        pub.publish(mk_event(i))
    stats = pub.flush()
    assert stats["sent"] == 3
    assert pub.store.count() == 0
    assert len(broker.published) == 1
    topic, payload, qos = broker.published[0]
    assert topic == "t/t_demo/1/edge-1a/events"
    assert qos == 1
    body = json.loads(payload)
    assert [ev["envelope"]["seq_no"] for ev in body["events"]] == [1, 2, 3]
    assert body["events"][0]["type"] == "track_update"
    pub.close()


def test_mqtt_broker_down_spools_then_drains(tmp_path, monkeypatch):
    """Spool/at-least-once taşımadan bağımsız: broker kapalıyken olaylar
    spool'da bekler, broker dönünce seq sırasıyla boşaltılır."""
    broker = install_fake_paho(monkeypatch, FakeBroker())
    broker.down = True
    pub = Publisher(mk_config(), spool_path=tmp_path / "spool.sqlite")
    for i in range(3):
        pub.publish(mk_event(i))
    stats = pub.flush()
    assert stats["sent"] == 0
    assert stats["dropped"] == 0  # MQTT'de kalıcı red yok: her hata geçici
    assert pub.store.count() == 3

    broker.down = False
    pub.publish(mk_event(3))
    stats = pub.flush()
    assert stats["sent"] == 4
    assert pub.store.count() == 0
    all_events = [ev for _, payload, _ in broker.published for ev in json.loads(payload)["events"]]
    assert [ev["envelope"]["seq_no"] for ev in all_events] == [1, 2, 3, 4]
    pub.close()


def test_mqtt_qos_from_config(tmp_path, monkeypatch):
    broker = install_fake_paho(monkeypatch, FakeBroker())
    pub = Publisher(mk_config(qos=2), spool_path=tmp_path / "spool.sqlite")
    pub.publish(mk_event(0))
    pub.flush()
    assert broker.published[0][2] == 2
    pub.close()


def test_mqtt_without_paho_gives_clear_error(tmp_path, monkeypatch):
    """paho kurulu değilken transport: mqtt anlaşılır kurulum ipucu vermeli."""
    monkeypatch.setitem(sys.modules, "paho", None)  # import'u ImportError'a zorla
    monkeypatch.setitem(sys.modules, "paho.mqtt", None)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", None)
    with pytest.raises(ImportError, match=r"wherugo-edge\[mqtt\]"):
        Publisher(mk_config(), spool_path=tmp_path / "spool.sqlite")


def test_make_transport_rejects_unknown_kind():
    cfg = mk_config()
    cfg.publisher.transport = "pigeon"  # config doğrulamasını atlayan programatik değer
    with pytest.raises(ValueError, match="pigeon"):
        make_transport(cfg)
