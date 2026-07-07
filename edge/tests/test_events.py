"""Olay serileştirme: zarf alanları eksiksiz, alan adları CONTRACTS §2 ile birebir."""
import json
import random
import uuid
from datetime import datetime, timezone

from wherugo_edge.events import (
    SCHEMA_VERSION,
    Enveloper,
    InteractionDetected,
    Quality,
    QueueMeasurement,
    TrackUpdate,
    ZoneEnter,
    ZoneExit,
)

TS = datetime(2026, 7, 7, 14, 3, 22, 150000, tzinfo=timezone.utc)

ENVELOPE_KEYS = [
    "tenant_id",
    "store_id",
    "device_id",
    "schema_version",
    "seq_no",
    "event_id",
    "event_time",
    "ingest_time",
]


def make_enveloper(**kw):
    return Enveloper("t_demo", 1, "edge-1a", **kw)


def sample_events():
    return [
        TrackUpdate(
            event_time=TS, camera_id=4, track_id=90211, x_m=14.2, y_m=6.8,
            sigma_cm=22.5, is_staff=False,
            quality=Quality(conf=0.91, occlusion_ratio=0.12, coverage_ok=True),
        ),
        ZoneEnter(
            event_time=TS, zone_id=31, zone_type="shelf", track_id=90211,
            entry_edge="south",
            quality=Quality(conf=0.88, id_switch_risk=0.05, coverage_ok=True),
        ),
        ZoneExit(
            event_time=TS, zone_id=31, track_id=90211, dwell_sec=47.3,
            classification="dwell",
            quality=Quality(conf=0.90, id_switch_risk=0.05, coverage_ok=True),
        ),
        InteractionDetected(
            event_time=TS, zone_id=31, track_id=90211, duration_sec=8.1,
            quality=Quality(conf=0.61, id_switch_risk=0.10, coverage_ok=True),
        ),
        QueueMeasurement(
            event_time=TS, zone_id=7, queue_len=6, est_wait_sec=210,
            joins_since_last=2, abandons_since_last=1, active_checkouts=2,
            quality=Quality(conf=0.86, coverage_ok=True),
        ),
    ]


def test_envelope_fields_complete_and_monotonic_seq():
    env = make_enveloper()
    wires = [env.wrap(ev) for ev in sample_events()]
    for i, wire in enumerate(wires):
        e = wire["envelope"]
        assert list(e.keys()) == ENVELOPE_KEYS
        assert e["tenant_id"] == "t_demo"
        assert e["store_id"] == 1
        assert e["device_id"] == "edge-1a"
        assert e["schema_version"] == SCHEMA_VERSION == 3
        assert e["seq_no"] == i + 1
        uuid.UUID(e["event_id"])  # geçerli UUID
        assert e["event_time"] == "2026-07-07T14:03:22.150Z"
        assert e["ingest_time"] is None


def test_track_update_payload_fields():
    wire = make_enveloper().wrap(sample_events()[0])
    assert wire["type"] == "track_update"
    assert wire["camera_id"] == 4
    assert wire["track_id"] == 90211
    assert wire["pos"] == {"x_m": 14.2, "y_m": 6.8, "sigma_cm": 22.5}
    assert wire["quality"] == {"conf": 0.91, "occlusion_ratio": 0.12, "coverage_ok": True}
    assert wire["is_staff"] is False


def test_zone_events_payload_fields():
    env = make_enveloper()
    enter = env.wrap(sample_events()[1])
    assert enter["type"] == "zone_enter"
    assert enter["zone_id"] == 31
    assert enter["zone_type"] == "shelf"
    assert enter["entry_edge"] == "south"
    assert enter["quality"]["id_switch_risk"] == 0.05

    exit_ = env.wrap(sample_events()[2])
    assert exit_["type"] == "zone_exit"
    assert exit_["dwell_sec"] == 47.3
    assert exit_["classification"] == "dwell"

    inter = env.wrap(sample_events()[3])
    assert inter["type"] == "interaction_detected"
    assert inter["interaction"] == "interaction_candidate"
    assert inter["duration_sec"] == 8.1
    assert "clip_ref" not in inter  # yoksa yazılmaz


def test_queue_measurement_payload_fields():
    wire = make_enveloper().wrap(sample_events()[4])
    assert wire["type"] == "queue_measurement"
    assert wire["zone_type"] == "queue"
    assert wire["queue_len"] == 6
    assert wire["est_wait_sec"] == 210
    assert wire["joins_since_last"] == 2
    assert wire["abandons_since_last"] == 1
    assert wire["active_checkouts"] == 2


def test_wire_is_json_serializable():
    env = make_enveloper()
    for ev in sample_events():
        text = json.dumps(env.wrap(ev), ensure_ascii=False)
        assert json.loads(text)["envelope"]["schema_version"] == 3


def test_seeded_rng_gives_deterministic_event_ids():
    a = make_enveloper(rng=random.Random(7))
    b = make_enveloper(rng=random.Random(7))
    evs = sample_events()
    ids_a = [a.wrap(ev)["envelope"]["event_id"] for ev in evs]
    ids_b = [b.wrap(ev)["envelope"]["event_id"] for ev in evs]
    assert ids_a == ids_b
    assert len(set(ids_a)) == len(ids_a)
