"""Piksel -> plan (metre) projeksiyonu: 3x3 homografi, saf Python."""
from __future__ import annotations

from typing import Sequence

Matrix = list[list[float]]


def _det3(m: Matrix) -> float:
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


class Homography:
    """3x3 homografi matrisi ile piksel (u,v) -> plan (x_m, y_m)."""

    def __init__(self, matrix: Sequence[Sequence[float]]) -> None:
        if len(matrix) != 3 or any(len(row) != 3 for row in matrix):
            raise ValueError("homography 3x3 matris olmalı")
        try:
            self.m: Matrix = [[float(v) for v in row] for row in matrix]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"homography sayısal olmalı: {exc}") from exc
        if abs(_det3(self.m)) < 1e-12:
            raise ValueError("homography tekil (det ~ 0)")

    @classmethod
    def identity(cls) -> "Homography":
        return cls([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])

    def project(self, u: float, v: float) -> tuple[float, float]:
        m = self.m
        w = m[2][0] * u + m[2][1] * v + m[2][2]
        if abs(w) < 1e-9:
            raise ValueError(f"projeksiyon sonsuzda: ({u}, {v})")
        x = (m[0][0] * u + m[0][1] * v + m[0][2]) / w
        y = (m[1][0] * u + m[1][1] * v + m[1][2]) / w
        return (x, y)
