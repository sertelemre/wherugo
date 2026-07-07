"""Homografi: 3x3 projeksiyon, normalizasyon, doğrulama."""
import pytest

from wherugo_edge.homography import Homography


def test_identity():
    h = Homography.identity()
    assert h.project(3.5, 7.25) == (3.5, 7.25)


def test_affine_scale_translate():
    h = Homography([[2, 0, 1], [0, 3, 2], [0, 0, 1]])
    assert h.project(2, 2) == (5, 8)


def test_perspective_normalization():
    h = Homography([[1, 0, 0], [0, 1, 0], [0, 0, 2]])
    assert h.project(1, 1) == (0.5, 0.5)


def test_pixel_to_plan_demo_matrix():
    # 1920x1080 -> 20x12 m (deploy/edge-demo.yaml ile aynı biçim)
    h = Homography([[20 / 1920, 0, 0], [0, -12 / 1080, 12], [0, 0, 1]])
    x, y = h.project(960, 1080)
    assert abs(x - 10.0) < 1e-9
    assert abs(y - 0.0) < 1e-9


def test_invalid_shapes_and_singular():
    with pytest.raises(ValueError):
        Homography([[1, 0], [0, 1]])
    with pytest.raises(ValueError):
        Homography([[0, 0, 0], [0, 0, 0], [0, 0, 0]])
