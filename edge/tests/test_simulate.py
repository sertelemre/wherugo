"""Simülatör: seed determinizmi + olay karışımının makullüğü."""
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wherugo_edge.config import load_config
from wherugo_edge.events import Enveloper, rfc3339
from wherugo_edge.sources.simulate import run_simulation

DEMO_YAML = Path(__file__).resolve().parents[2] / "deploy" / "edge-demo.yaml"
START = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)


def collect(seed, duration=300):
    cfg = load_config(DEMO_YAML)
    enveloper = Enveloper(cfg.tenant_id, cfg.store_id, cfg.device_id, rng=random.Random(seed))
    wires = []
    for _, events in run_simulation(cfg, seed=seed, duration_sec=duration, start_time=START):
        for ev in events:
            wires.append(enveloper.wrap(ev))
    return wires


def test_same_seed_same_event_sequence():
    a = collect(seed=42)
    b = collect(seed=42)
    assert len(a) == len(b)
    assert a == b  # zarf (event_id dahil) + payload birebir aynı


def test_different_seed_differs():
    a = collect(seed=42, duration=120)
    b = collect(seed=43, duration=120)
    assert a != b


def test_event_mix_is_sane():
    wires = collect(seed=7, duration=600)
    by_type = {}
    for w in wires:
        by_type.setdefault(w["type"], []).append(w)

    assert len(by_type.get("track_update", [])) > 100
    assert len(by_type.get("zone_enter", [])) >= 3
    assert len(by_type.get("zone_exit", [])) >= 3
    assert len(by_type.get("interaction_detected", [])) >= 1
    # 600 sn / 30 sn periyot: ilk periyot başlangıç, ~19 ölçüm
    assert len(by_type["queue_measurement"]) >= 15

    # zarf monoton seq + schema_version
    seqs = [w["envelope"]["seq_no"] for w in wires]
    assert seqs == list(range(1, len(wires) + 1))
    assert all(w["envelope"]["schema_version"] == 3 for w in wires)
    assert all(w["envelope"]["event_time"].endswith("Z") for w in wires)

    # zone_enter/exit eşleşmesi: her (zone, track) için exit sayısı <= enter sayısı
    opens = {}
    for w in wires:
        key = (w.get("zone_id"), w.get("track_id"))
        if w["type"] == "zone_enter":
            opens[key] = opens.get(key, 0) + 1
        elif w["type"] == "zone_exit":
            opens[key] = opens.get(key, 0) - 1
            assert opens[key] >= 0, f"exit'i olmayan enter: {key}"

    # dwell/pass_by sınıflandırması eşikle tutarlı
    for w in by_type["zone_exit"]:
        if w["classification"] == "dwell":
            assert w["dwell_sec"] >= 5.0
        else:
            assert w["dwell_sec"] < 5.0

    # kuyruk ölçümleri makul
    for w in by_type["queue_measurement"]:
        assert w["queue_len"] >= 0
        assert w["est_wait_sec"] >= 0
        assert w["zone_id"] == 7

    # staff track'leri var mı (staff_ratio=0.1, 600 sn'de garantili değil ama
    # is_staff alanı her track_update'te bulunmalı)
    assert all("is_staff" in w for w in by_type["track_update"])


def test_event_times_monotonic_per_step():
    wires = collect(seed=11, duration=120)
    times = [w["envelope"]["event_time"] for w in wires if w["type"] == "queue_measurement"]
    assert times == sorted(times)
    # periyot ilk sim adımında (START+1) kurulur; ilk ölçüm +31 sn'de
    assert times[0] == rfc3339(START + timedelta(seconds=31))
