"""MQTT -> ingest köprüsü (CONTRACTS §13, üretim taşıma yolu).

Çalıştırma: ``python -m wherugo_backend.mqtt_bridge``

- ``WHERUGO_MQTT_HOST`` (vars. localhost) / ``WHERUGO_MQTT_PORT`` (vars. 1883)
  adresindeki broker'a bağlanır, ``t/+/+/+/events`` konusuna QoS 1 abone olur.
- Payload HTTP ingest ile aynı JSON batch'tir: ``{"events": [...]}``; her mesaj
  kendi DB session'ıyla :func:`wherugo_backend.ingest.process_batch`'e verilir
  (idempotens ve seq-gap davranışı HTTP yoluyla birebir aynı).
- Auth tenant'ı olay zarfından DEĞİL, topic'in ``{tenant}`` segmentinden gelir
  (``t/{tenant_id}/{store_id}/{device_id}/events``) — MQTT tarafında broker ACL
  bu segmenti garanti eder (üretimde mTLS + topic ACL, docs/02).
- Bozuk mesaj (geçersiz topic/JSON/gövde) loglanır ve atlanır; batch düşmez.
- Yeniden bağlanma paho'nun kendi mekanizmasıdır (``reconnect_delay_set`` +
  ``loop_forever``); abonelik ``on_connect`` içinde yapıldığından her yeniden
  bağlanışta kendiliğinden tazelenir.

paho-mqtt ``backend[mqtt]`` extra'sıyla kurulur; kurulu değilse köprü anlaşılır
bir hata basıp 2 koduyla çıkar. Bu modülün import'u paho GEREKTİRMEZ — mesaj
işleme (:func:`handle_message`) paho'suz birim test edilebilir.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Callable

from sqlalchemy.orm import Session

from .db import Base, create_db_engine, create_session_factory
from .ingest import IngestResult, process_batch

log = logging.getLogger("wherugo.mqtt_bridge")

TOPIC_FILTER = "t/+/+/+/events"
QOS = 1
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 1883


def tenant_from_topic(topic: str) -> str | None:
    """``t/{tenant_id}/{store_id}/{device_id}/events`` -> tenant_id, yoksa None."""
    parts = topic.split("/")
    if len(parts) != 5 or parts[0] != "t" or parts[4] != "events":
        return None
    tenant = parts[1]
    return tenant or None


def parse_payload(payload: bytes | str) -> list[dict[str, Any]] | None:
    """JSON ``{"events": [...]}`` gövdesini çözer; bozuksa None."""
    try:
        body = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    events = body.get("events")
    if not isinstance(events, list):
        return None
    return events


def handle_message(session_factory: Callable[[], Session], topic: str,
                   payload: bytes | str) -> IngestResult | None:
    """Tek bir MQTT mesajını işler (on_message'ın paho'suz test edilebilir çekirdeği).

    Tenant topic'ten alınır, batch ``process_batch``'e verilir. Bozuk mesaj
    loglanıp atlanır (None döner); başarıda IngestResult döner.
    """
    tenant_id = tenant_from_topic(topic)
    if tenant_id is None:
        log.warning("gecersiz topic, mesaj atlandi: %r", topic)
        return None
    events = parse_payload(payload)
    if events is None:
        log.warning("bozuk payload (JSON {'events': [...]} degil), mesaj atlandi: topic=%s", topic)
        return None
    try:
        with session_factory() as session:
            result = process_batch(session, tenant_id, events)
    except Exception:  # tek zehirli mesaj köprüyü öldürmemeli
        log.exception("ingest hatasi, mesaj atlandi: topic=%s", topic)
        return None
    log.info("topic=%s accepted=%d duplicates=%d rejected=%d gap=%s",
             topic, result.accepted, result.duplicates, result.rejected, result.gap_detected)
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("WHERUGO_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("HATA: paho-mqtt kurulu değil. Kurulum: pip install 'wherugo-backend[mqtt]' "
              "(veya: pip install 'paho-mqtt>=2')", file=sys.stderr)
        return 2

    host = os.environ.get("WHERUGO_MQTT_HOST", DEFAULT_HOST)
    port = int(os.environ.get("WHERUGO_MQTT_PORT", str(DEFAULT_PORT)))

    engine = create_db_engine()
    # İdempotent: backend zaten create_all yapar; köprü backend'den önce
    # başlarsa da tablolar hazır olsun.
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                         client_id="wherugo-mqtt-bridge")
    # Yeniden bağlanma: paho'nun kendi üstel geri çekilmesi.
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    def on_connect(client, userdata, flags, reason_code, properties):
        log.info("broker'a baglandi (%s:%d, rc=%s); abone olunuyor: %s qos=%d",
                 host, port, reason_code, TOPIC_FILTER, QOS)
        # Abonelik burada: her yeniden bağlanışta otomatik tazelenir.
        client.subscribe(TOPIC_FILTER, qos=QOS)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        log.warning("broker baglantisi koptu (rc=%s); paho yeniden deneyecek", reason_code)

    def on_message(client, userdata, msg):
        handle_message(session_factory, msg.topic, msg.payload)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    log.info("wherugo mqtt köprüsü başlıyor: broker=%s:%d topic=%s", host, port, TOPIC_FILTER)
    # connect_async + retry_first_connection: broker köprüden sonra ayağa
    # kalksa bile süreç düşmez, paho bağlanana dek dener.
    client.connect_async(host, port, keepalive=60)
    client.loop_forever(retry_first_connection=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
