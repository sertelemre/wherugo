"""Gizlilik değişmezleri (CONTRACTS §0/§6).

Olay veri yolunda piksel/frame taşıyan hiçbir alan olamaz — bu, politika
değil şema düzeyinde imkânsızlıktır. Bu modül hem olay tiplerini hem de
tel'e (wire) giden her JSON'u doğrular.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from . import events as _events

FORBIDDEN_KEY_TOKENS = (
    "frame",
    "image",
    "pixel",
    "jpeg",
    "jpg",
    "png",
    "bmp",
    "bitmap",
    "snapshot",
    "thumbnail",
    "face",
    "embedding",
)


class PrivacyViolation(AssertionError):
    """Olay veri yolunda piksel/görüntü verisi tespit edildi."""


def _check_key(key: str, path: str) -> None:
    low = key.lower()
    for token in FORBIDDEN_KEY_TOKENS:
        if token in low:
            raise PrivacyViolation(f"yasak alan adı '{key}' ({path}): '{token}' piksel/görüntü ima eder")


def assert_wire_safe(obj: Any, path: str = "$") -> None:
    """Tel'e gidecek nesnede bayt/piksel verisi olmadığını doğrular."""
    if isinstance(obj, (bytes, bytearray, memoryview)):
        raise PrivacyViolation(f"ham bayt verisi olay yoluna giremez ({path})")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _check_key(str(k), path)
            assert_wire_safe(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            assert_wire_safe(v, f"{path}[{i}]")


def assert_event_types_pixel_free() -> None:
    """Olay dataclass tanımlarında görüntü taşıyabilecek alan olmadığını doğrular."""
    classes = list(_events.EVENT_CLASSES) + [_events.Quality, _events.Envelope]
    for cls in classes:
        for f in dataclasses.fields(cls):
            _check_key(f.name, f"{cls.__name__}")
            ann = str(f.type)
            if "bytes" in ann or "memoryview" in ann or "ndarray" in ann:
                raise PrivacyViolation(
                    f"{cls.__name__}.{f.name}: bayt/dizi tipli alan olay şemasında olamaz ({ann})"
                )


# İçe aktarım anında şema düzeyi kontrol — tip tanımı bozulursa paket yüklenemez.
assert_event_types_pixel_free()
