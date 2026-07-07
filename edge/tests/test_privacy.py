"""Gizlilik değişmezleri: olay tiplerinde/tel'de piksel alanı yok."""
from datetime import datetime, timezone

import pytest

from wherugo_edge import privacy
from wherugo_edge.events import Enveloper, Quality, TrackUpdate


def test_event_dataclasses_are_pixel_free():
    privacy.assert_event_types_pixel_free()  # istisna atmamalı


def test_real_wire_event_passes():
    ev = TrackUpdate(
        event_time=datetime(2026, 7, 7, tzinfo=timezone.utc),
        camera_id=1, track_id=1, x_m=1.0, y_m=2.0, sigma_cm=20.0,
        is_staff=False, quality=Quality(conf=0.9),
    )
    wire = Enveloper("t_demo", 1, "edge-1a").wrap(ev)
    privacy.assert_wire_safe(wire)


@pytest.mark.parametrize(
    "bad",
    [
        {"frame": "..."},
        {"payload": {"image_b64": "abc"}},
        {"nested": [{"pixels": [0, 1]}]},
        {"snapshot_ref": "x"},
        {"face_embedding": [0.1]},
    ],
)
def test_forbidden_keys_raise(bad):
    with pytest.raises(privacy.PrivacyViolation):
        privacy.assert_wire_safe(bad)


def test_raw_bytes_raise():
    with pytest.raises(privacy.PrivacyViolation):
        privacy.assert_wire_safe({"pos": {"x_m": b"\x00\x01"}})
