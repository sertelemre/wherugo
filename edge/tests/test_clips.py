"""Klip çıkarımı + VLM hakemliği (CONTRACTS §15, edge tarafı).

Klipler yalnız YEREL diske yazılır (8 jpeg, 72 sa TTL); tel'e yalnız
metin/float verdict alanları çıkar. VLM çağrısı mock'lanır (ağa çıkılmaz);
simulate yolunda klip yolu hiç çalışmaz.
"""
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from wherugo_edge import privacy
from wherugo_edge.clips import ClipRecorder, judge_interaction, resolve_vlm_judge
from wherugo_edge.config import CameraDef, EdgeConfig, VideoParams, ZoneDef
from wherugo_edge.events import Enveloper, InteractionDetected, Quality
from wherugo_edge.homography import Homography
from wherugo_edge.main import _video_loop, main
from wherugo_edge.zones import TrackSample, ZoneEngine

T0 = datetime(2026, 7, 7, 10, 0, 0, tzinfo=timezone.utc)
DEMO_YAML = Path(__file__).resolve().parents[2] / "deploy" / "edge-demo.yaml"


def frames_seq(n, start=T0, step_sec=1.0):
    return [
        (start + timedelta(seconds=i * step_sec), np.full((24, 32, 3), 10 * i % 255, dtype=np.uint8))
        for i in range(n)
    ]


# -- ClipRecorder -----------------------------------------------------------


def test_save_clip_writes_8_jpegs_in_contract_layout(tmp_path):
    rec = ClipRecorder(tmp_path / "clips")
    frames = frames_seq(12)  # 11 sn'lik pencere, 12 kare -> 8 seçilir
    when = frames[-1][0]
    clip = rec.save_clip(frames, seq=42, when=when)
    assert clip == tmp_path / "clips" / "2026-07-07" / "evt-42"
    names = sorted(p.name for p in clip.iterdir())
    assert names == [f"frame-{n}.jpg" for n in range(1, 9)]
    assert all((clip / n).stat().st_size > 0 for n in names)


def test_save_clip_with_few_frames_writes_them_all(tmp_path):
    rec = ClipRecorder(tmp_path / "clips")
    clip = rec.save_clip(frames_seq(3), seq=1, when=T0 + timedelta(seconds=2))
    assert len(list(clip.glob("frame-*.jpg"))) == 3


def test_save_clip_without_frames_returns_none(tmp_path):
    rec = ClipRecorder(tmp_path / "clips")
    assert rec.save_clip([], seq=1, when=T0) is None
    assert not (tmp_path / "clips").exists()  # boşuna dizin açılmaz


def _make_clip_dir(root, day, seq, age_hours, now):
    d = root / day / f"evt-{seq}"
    d.mkdir(parents=True)
    (d / "frame-1.jpg").write_bytes(b"x")
    old = (now - timedelta(hours=age_hours)).timestamp()
    os.utime(d, (old, old))
    return d


def test_ttl_cleanup_runs_at_startup(tmp_path):
    """72 saati aşan klipler kurulumda silinir; yeniler kalır."""
    root = tmp_path / "clips"
    now = datetime.now(timezone.utc)
    stale = _make_clip_dir(root, "2026-07-01", 1, age_hours=80, now=now)
    fresh = _make_clip_dir(root, "2026-07-07", 2, age_hours=1, now=now)
    ClipRecorder(root, ttl_hours=72)
    assert not stale.exists()
    assert not stale.parent.exists()  # boşalan tarih dizini de kalkar
    assert fresh.exists()


def test_ttl_cleanup_repeats_hourly(tmp_path):
    root = tmp_path / "clips"
    now = datetime.now(timezone.utc)
    rec = ClipRecorder(root, ttl_hours=72, now=now)
    stale = _make_clip_dir(root, "2026-07-02", 3, age_hours=100, now=now)
    assert rec.maybe_cleanup(now + timedelta(minutes=30)) == 0  # saat dolmadı
    assert stale.exists()
    assert rec.maybe_cleanup(now + timedelta(minutes=61)) == 1  # saatlik temizlik
    assert not stale.exists()


# -- VLM hakemliği ----------------------------------------------------------


class FakeJudge:
    def __init__(self, verdict=None, error=None):
        self.calls = []
        self.verdict = verdict or SimpleNamespace(label="pickup", conf=0.83, rationale="test")
        self.error = error

    def judge_clip(self, clip_path, context):
        self.calls.append((clip_path, context))
        if self.error is not None:
            raise self.error
        return self.verdict


def mk_interaction():
    return InteractionDetected(
        event_time=T0, zone_id=10, track_id=7, duration_sec=8.2, quality=Quality(conf=0.6)
    )


def test_resolve_vlm_judge_skipped_without_env(monkeypatch):
    monkeypatch.delenv("WHERUGO_VLM_BASE_URL", raising=False)
    assert resolve_vlm_judge() is None


def test_resolve_vlm_judge_uses_wherugo_ai_factory(monkeypatch):
    monkeypatch.setenv("WHERUGO_VLM_BASE_URL", "http://gpu-host:8002/v1")
    judge = FakeJudge()
    fake_vlm = types.ModuleType("wherugo_ai.vlm")
    fake_vlm.get_vlm = lambda: judge
    monkeypatch.setitem(sys.modules, "wherugo_ai.vlm", fake_vlm)
    assert resolve_vlm_judge() is judge


def test_resolve_vlm_judge_survives_import_failure(monkeypatch, capsys):
    """ai paketi yok/uyumsuz: edge çökmez, hakemlik atlanır (CONTRACTS §1)."""
    monkeypatch.setenv("WHERUGO_VLM_BASE_URL", "http://gpu-host:8002/v1")
    monkeypatch.setitem(sys.modules, "wherugo_ai", None)
    monkeypatch.setitem(sys.modules, "wherugo_ai.vlm", None)
    assert resolve_vlm_judge() is None
    assert "klip hakemliği atlanıyor" in capsys.readouterr().err


def test_judge_interaction_attaches_text_and_float_only(tmp_path):
    judge = FakeJudge()
    ev = mk_interaction()
    judge_interaction(judge, ev, tmp_path / "evt-1")
    assert ev.vlm_verdict == "pickup"
    assert ev.vlm_conf == 0.83
    assert judge.calls[0][1]["event_type"] == "interaction_candidate"
    payload = ev.payload()
    assert payload["vlm_verdict"] == "pickup"
    assert payload["vlm_conf"] == 0.83
    privacy.assert_wire_safe(payload)  # metin/float izinli, piksel değil


def test_judge_interaction_swallows_vlm_errors(tmp_path, capsys):
    ev = mk_interaction()
    judge_interaction(FakeJudge(error=RuntimeError("vLLM timeout")), ev, tmp_path / "evt-1")
    assert ev.vlm_verdict is None and ev.vlm_conf is None
    assert "verdict'siz" in capsys.readouterr().err
    assert "vlm_verdict" not in ev.payload()


# -- video döngüsü uçtan uca: interaction -> klip + verdict ------------------


class StubVideoSource:
    """samples() + recent_frames() sözleşmesini sağlayan sahte kaynak."""

    def __init__(self, samples, frames):
        self._samples = samples
        self._frames = frames

    def samples(self):
        yield from self._samples

    def recent_frames(self, now):
        cutoff = now - timedelta(seconds=10.0)
        return [(ts, f) for ts, f in self._frames if ts >= cutoff]


def test_video_loop_saves_clip_and_verdict_on_interaction(tmp_path, capsys):
    shelf = ZoneDef(id=10, name="raf", zone_type="shelf", polygon=[(0, 0), (10, 0), (10, 10), (0, 10)])
    engine = ZoneEngine([shelf], hysteresis_samples=1, dwell_threshold_sec=1.0,
                        interaction_dwell_sec=2.0)
    samples = [
        TrackSample(track_id=1, ts=T0 + timedelta(seconds=i), x_m=5.0, y_m=5.0)
        for i in range(4)
    ]  # 3 sn raf içinde -> interaction (eşik 2 sn)
    src = StubVideoSource(samples, frames_seq(12, start=T0 - timedelta(seconds=8)))
    recorder = ClipRecorder(tmp_path / "clips")
    judge = FakeJudge()
    enveloper = Enveloper("t_demo", 1, "edge-1a")
    args = SimpleNamespace(duration=0)

    _video_loop(src, engine, None, enveloper, args, started=0.0,
                recorder=recorder, vlm_judge=judge)

    wires = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    inter = [w for w in wires if w["type"] == "interaction_detected"]
    assert len(inter) == 1
    w = inter[0]
    # klip zarfın seq'iyle adlandırılır ve YEREL diske yazılır
    clip_dir = tmp_path / "clips" / w["clip_ref"]
    assert clip_dir.name == f"evt-{w['envelope']['seq_no']}"
    jpgs = list(clip_dir.glob("frame-*.jpg"))
    assert 1 <= len(jpgs) <= 8
    # verdict tel'de yalnız metin/float olarak
    assert w["vlm_verdict"] == "pickup"
    assert w["vlm_conf"] == 0.83
    assert judge.calls and judge.calls[0][0] == str(clip_dir)
    for wire in wires:
        privacy.assert_wire_safe(wire)


def test_video_loop_without_judge_still_saves_clip(tmp_path, capsys):
    shelf = ZoneDef(id=10, name="raf", zone_type="shelf", polygon=[(0, 0), (10, 0), (10, 10), (0, 10)])
    engine = ZoneEngine([shelf], hysteresis_samples=1, dwell_threshold_sec=1.0,
                        interaction_dwell_sec=2.0)
    samples = [
        TrackSample(track_id=1, ts=T0 + timedelta(seconds=i), x_m=5.0, y_m=5.0)
        for i in range(4)
    ]
    src = StubVideoSource(samples, frames_seq(5))
    recorder = ClipRecorder(tmp_path / "clips")
    enveloper = Enveloper("t_demo", 1, "edge-1a")
    _video_loop(src, engine, None, enveloper, SimpleNamespace(duration=0), started=0.0,
                recorder=recorder, vlm_judge=None)
    wires = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    w = next(w for w in wires if w["type"] == "interaction_detected")
    assert "clip_ref" in w and "vlm_verdict" not in w  # klip var, verdict yok


def test_simulate_path_never_touches_clips(tmp_path, monkeypatch, capsys):
    """Simulate yolunda klip yolu HİÇ çalışmaz: clips/ dizini oluşmaz (piksel yok)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WHERUGO_VLM_BASE_URL", "http://gpu-host:8002/v1")  # dolu olsa bile
    rc = main([
        "--config", str(DEMO_YAML), "--dry-run",
        "--duration", "40", "--speed", "0", "--seed", "7",
    ])
    assert rc == 0
    assert capsys.readouterr().out.strip()
    assert not (tmp_path / "clips").exists()
