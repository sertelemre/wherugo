"""Video yolu GPU'suz (CONTRACTS §16): mock dedektör (MOG2 + kontur) +
centroid takibi + homografi -> zone motoru, sentetik karelerle uçtan uca.

torch/ultralytics KULLANILMAZ; yalnız opencv-python-headless + numpy.
"""
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from wherugo_edge.config import CameraDef, EdgeConfig, VideoParams, ZoneDef
from wherugo_edge.homography import Homography
from wherugo_edge.sources.video import (
    CentroidTracker,
    Detection,
    MockDetector,
    VideoSource,
    open_source,
)
from wherugo_edge.zones import ZoneEngine

T0 = datetime(2026, 7, 7, 10, 0, 0, tzinfo=timezone.utc)

# Kare: 400x240 piksel; homografi 0.05 m/px -> 20x12 m plan (y çevrilmez: test sadeliği).
FRAME_W, FRAME_H = 400, 240
SCALE = 0.05
HOMOGRAPHY = Homography([[SCALE, 0.0, 0.0], [0.0, SCALE, 0.0], [0.0, 0.0, 1.0]])

# Shelf zone plan koordinatında (6..14 m, 3..9 m) = piksel (120..280, 60..180).
SHELF = ZoneDef(id=10, name="raf", zone_type="shelf", polygon=[(6, 3), (14, 3), (14, 9), (6, 9)])

PERSON_W, PERSON_H = 20, 40
PERSON_TOP = 100  # y2 = 140 px -> y_m = 7.0 (zone içinde)


def mk_config(**video_kwargs):
    return EdgeConfig(
        tenant_id="t_demo",
        store_id=1,
        device_id="edge-1a",
        backend_url="http://127.0.0.1:9",
        cameras=[CameraDef(id=1, name="cam", homography=HOMOGRAPHY)],
        zones=[SHELF],
        video=VideoParams(detector="mock", **video_kwargs),
    )


def background():
    return np.full((FRAME_H, FRAME_W, 3), 30, dtype=np.uint8)


def frame_with_person(center_u):
    """Arka plan + kişi-vari parlak dikdörtgen (ayak noktası: alt-orta)."""
    f = background()
    x1 = int(center_u - PERSON_W / 2)
    f[PERSON_TOP:PERSON_TOP + PERSON_H, max(0, x1):max(0, x1) + PERSON_W] = 220
    return f


def synthetic_walk(warmup=15, xs=None, step_sec=1.0):
    """(ts, frame) dizisi: MOG2 ısınması için statik arka plan, sonra
    soldan sağa yürüyen dikdörtgen (1 m/kare, 1 kare/sn -> 1 m/sn)."""
    xs = xs if xs is not None else list(range(20, 400, 20))
    frames = []
    i = 0
    for _ in range(warmup):
        frames.append((T0 + timedelta(seconds=i * step_sec), background()))
        i += 1
    for u in xs:
        frames.append((T0 + timedelta(seconds=i * step_sec), frame_with_person(u)))
        i += 1
    return frames


# -- MockDetector -----------------------------------------------------------


def test_mock_detector_finds_moving_blob():
    det = MockDetector()
    for ts, frame in synthetic_walk(warmup=15, xs=[100]):
        found = det.detect(frame)
    assert len(found) == 1
    d = found[0]
    # bbox hareketli dikdörtgeni sarmalı (morfoloji birkaç piksel genişletebilir)
    assert abs((d.x1 + d.x2) / 2 - 100) <= 8
    assert abs(d.y2 - (PERSON_TOP + PERSON_H)) <= 8
    assert 0.0 < d.conf <= 1.0


def test_mock_detector_ignores_static_background():
    det = MockDetector()
    detections = [det.detect(f) for _, f in synthetic_walk(warmup=12, xs=[])]
    # ısınma sonrası statik sahnede tespit olmamalı
    assert detections[-1] == []


# -- CentroidTracker --------------------------------------------------------


def box(cx, cy, w=20, h=40):
    return Detection(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def test_tracker_keeps_id_while_moving():
    tr = CentroidTracker()
    ids = []
    for cx in (100, 112, 124, 136):
        matched = tr.update([box(cx, 120)])
        assert len(matched) == 1
        ids.append(matched[0][0])
    assert len(set(ids)) == 1  # aynı nesne = aynı id


def test_tracker_tolerates_short_gaps_then_expires():
    tr = CentroidTracker(max_lost=3, max_dist_px=80.0)
    tid = tr.update([box(100, 120)])[0][0]
    for _ in range(2):  # kısa oklüzyon: tespit yok
        assert tr.update([]) == []
    assert tr.update([box(115, 120)])[0][0] == tid  # id korunur

    for _ in range(4):  # max_lost aşıldı: track düşer
        tr.update([])
    assert tr.update([box(115, 120)])[0][0] != tid  # yeni id


def test_tracker_separates_distant_objects():
    tr = CentroidTracker()
    m0 = tr.update([box(50, 120), box(300, 120)])
    assert len({tid for tid, _ in m0}) == 2
    m1 = tr.update([box(58, 120), box(292, 120)])
    by_pos = {round(d.centroid[0] / 100): tid for tid, d in m1}
    by_pos0 = {round(d.centroid[0] / 100): tid for tid, d in m0}
    assert by_pos == by_pos0  # id'ler çaprazlanmadı


# -- uçtan uca: kare -> mock dedektör -> homografi -> zone olayları ----------


def test_synthetic_walk_end_to_end_zone_events():
    """Sentetik yürüyüş GPU'suz uçtan uca zone_enter/zone_exit üretir."""
    cfg = mk_config()
    src = VideoSource(
        cfg, cfg.cameras[0], "synthetic", detector="mock",
        frames=iter(synthetic_walk()), sample_hz=10.0,
    )
    engine = ZoneEngine([SHELF], hysteresis_samples=2, dwell_threshold_sec=5.0,
                        interaction_dwell_sec=6.0)
    samples = []
    events = []
    for sample in src.samples():
        samples.append(sample)
        events.extend(engine.process(sample))

    assert samples, "mock dedektör hiç örnek üretmedi"
    # homografi uygulanmış plan koordinatları: y ~ 7.0 m, x soldan sağa artar
    ys = [s.y_m for s in samples]
    assert all(6.4 <= y <= 7.6 for y in ys), ys
    xs = [s.x_m for s in samples]
    assert xs == sorted(xs)  # tek yönlü yürüyüş
    assert len({s.track_id for s in samples}) == 1  # takip id'yi korudu

    enters = [e for e in events if type(e).__name__ == "ZoneEnter"]
    exits = [e for e in events if type(e).__name__ == "ZoneExit"]
    assert len(enters) == 1 and len(exits) == 1
    assert enters[0].zone_id == SHELF.id
    assert exits[0].zone_id == SHELF.id
    assert exits[0].track_id == enters[0].track_id
    # zone genişliği 8 m, hız ~1 m/sn -> dwell >= 5 sn: 'dwell' sınıfı
    assert exits[0].classification == "dwell"
    assert exits[0].dwell_sec >= 5.0

    interactions = [e for e in events if type(e).__name__ == "InteractionDetected"]
    assert len(interactions) == 1  # 6 sn raf önü -> interaction_candidate

    assert "ultralytics" not in sys.modules  # GPU'suz yol torch'a dokunmadı
    assert "torch" not in sys.modules


def test_open_source_mock_without_ultralytics(tmp_path):
    cfg = mk_config()
    src = open_source(cfg, "dummy.mp4", detector="mock", frames=iter([]))
    assert src.detector_kind == "mock"
    assert list(src.samples()) == []


def test_yolo_detector_missing_dependency_clear_error():
    """ultralytics kurulu değil: detector yolo anlaşılır kurulum ipucu vermeli."""
    cfg = mk_config()
    with pytest.raises(ImportError, match=r"wherugo-edge\[yolo\]"):
        open_source(cfg, "rtsp://demo", detector="yolo")


def test_unknown_detector_rejected():
    cfg = mk_config()
    with pytest.raises(ValueError, match="bilinmeyen detector"):
        VideoSource(cfg, cfg.cameras[0], "x", detector="hog")


def test_recent_frames_window():
    cfg = mk_config()
    src = VideoSource(cfg, cfg.cameras[0], "synthetic", detector="mock",
                      frames=iter([]), clip_window_sec=10.0)
    for i in range(20):
        src._remember_frame(T0 + timedelta(seconds=i), background())
    now = T0 + timedelta(seconds=19)
    recent = src.recent_frames(now)
    assert len(recent) == 11  # [now-10, now] penceresi
    assert recent[0][0] == T0 + timedelta(seconds=9)
