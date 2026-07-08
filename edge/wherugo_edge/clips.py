"""Klip çıkarımı + VLM hakemliği (CONTRACTS §15) — YALNIZ video modunda.

Gizlilik: klip kareleri yalnız YEREL diske yazılır
(``clips/<YYYY-MM-DD>/evt-<seq>/frame-N.jpg``, 72 saat TTL) ve mağaza ağını
terk etmez. Tel'e (publisher) yalnız metin/float verdict alanları çıkar
(``interaction_detected.vlm_verdict/vlm_conf`` — privacy.assert_wire_safe
bunlara izin verir; piksel ASLA). Simulate kaynağı bu modülü hiç kullanmaz.
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .events import InteractionDetected

FRAMES_PER_CLIP = 8          # CONTRACTS §15: en fazla 8 jpeg karesi
DEFAULT_TTL_HOURS = 72.0     # CONTRACTS §15: 72 saat TTL
CLEANUP_INTERVAL = timedelta(hours=1)  # başlangıçta + saatlik temizlik


def _evenly_spaced(items: list, k: int) -> list:
    """Listeden en fazla k öğeyi (ilk/son dahil) eşit aralıkla seçer."""
    if len(items) <= k:
        return list(items)
    step = (len(items) - 1) / (k - 1)
    idxs = sorted({round(i * step) for i in range(k)})
    return [items[i] for i in idxs]


class ClipRecorder:
    """Interaction anında ring buffer karelerini yerel jpeg klip olarak yazar.

    Dizin düzeni: ``<clip_dir>/<YYYY-MM-DD>/evt-<seq>/frame-1.jpg .. frame-8.jpg``.
    TTL temizliği: kurulumda bir kez + ``maybe_cleanup()`` ile saatlik.
    """

    def __init__(
        self,
        clip_dir: str | Path,
        *,
        ttl_hours: float = DEFAULT_TTL_HOURS,
        frames_per_clip: int = FRAMES_PER_CLIP,
        now: Optional[datetime] = None,
    ) -> None:
        self.dir = Path(clip_dir)
        self.ttl = timedelta(hours=float(ttl_hours))
        self.frames_per_clip = max(1, int(frames_per_clip))
        now = now or datetime.now(timezone.utc)
        self.cleanup(now)  # başlangıç temizliği (CONTRACTS §15)
        self._next_cleanup = now + CLEANUP_INTERVAL

    def save_clip(
        self, frames: list[tuple[datetime, Any]], *, seq: int, when: datetime
    ) -> Optional[Path]:
        """Kareleri ``<tarih>/evt-<seq>/`` altına yazar; kare yoksa None döner."""
        if not frames:
            return None
        try:
            import cv2
        except ImportError:  # klip jpeg kodlaması OpenCV ister; yoksa sessizce atla
            return None
        picks = _evenly_spaced(sorted(frames, key=lambda p: p[0]), self.frames_per_clip)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        day = when.astimezone(timezone.utc).strftime("%Y-%m-%d")
        clip_path = self.dir / day / f"evt-{int(seq)}"
        clip_path.mkdir(parents=True, exist_ok=True)
        written = 0
        for n, (_ts, frame) in enumerate(picks, start=1):
            if cv2.imwrite(str(clip_path / f"frame-{n}.jpg"), frame):
                written += 1
        return clip_path if written else None

    def maybe_cleanup(self, now: Optional[datetime] = None) -> int:
        """Saatte bir TTL temizliği çalıştırır (video döngüsünden çağrılır)."""
        now = now or datetime.now(timezone.utc)
        if now < self._next_cleanup:
            return 0
        self._next_cleanup = now + CLEANUP_INTERVAL
        return self.cleanup(now)

    def cleanup(self, now: Optional[datetime] = None) -> int:
        """TTL'i (vars. 72 sa) aşan evt dizinlerini siler; sayısını döndürür."""
        now = now or datetime.now(timezone.utc)
        cutoff = now.timestamp() - self.ttl.total_seconds()
        removed = 0
        if not self.dir.exists():
            return 0
        for date_dir in sorted(self.dir.iterdir()):
            if not date_dir.is_dir():
                continue
            for evt_dir in sorted(date_dir.iterdir()):
                if evt_dir.is_dir() and evt_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(evt_dir, ignore_errors=True)
                    removed += 1
            try:
                date_dir.rmdir()  # yalnız boşsa kalkar
            except OSError:
                pass
        return removed


# -- VLM hakemliği (opsiyonel; ai paketi bağımsız evrilir) ------------------


def resolve_vlm_judge() -> Optional[Any]:
    """WHERUGO_VLM_BASE_URL doluysa ``wherugo_ai.vlm.get_vlm()`` döndürür.

    ai paketi kurulu/uyumlu değilse (ImportError dahil her hata) None döner ve
    klip hakemliği atlanır — edge, ai olmadan da tam çalışır (CONTRACTS §1:
    edge hiçbir pakete bağımlı değildir; bu çağrı en-iyi-çaba eklentidir).
    """
    if not os.environ.get("WHERUGO_VLM_BASE_URL", "").strip():
        return None
    try:
        from wherugo_ai.vlm import get_vlm

        return get_vlm()
    except Exception as exc:  # ImportError, AttributeError, config hataları...
        print(
            f"uyarı: VLM istemcisi kurulamadı ({exc}); klip hakemliği atlanıyor",
            file=sys.stderr,
        )
        return None


def judge_interaction(judge: Any, event: InteractionDetected, clip_path: Path) -> None:
    """VLM verdict'ini olaya yazar (vlm_verdict/vlm_conf); her hatada atlar.

    Tel'e yalnız metin + float çıkar; klip yolu/pikseller yerelde kalır.
    """
    if judge is None or clip_path is None:
        return
    try:
        verdict = judge.judge_clip(
            str(clip_path),
            {
                "event_type": "interaction_candidate",
                "zone_id": int(event.zone_id),
                "duration_sec": float(event.duration_sec),
            },
        )
        event.vlm_verdict = str(verdict.label)
        event.vlm_conf = float(verdict.conf)
    except Exception as exc:  # ağ/parse/uyumsuz arayüz: olay verdict'siz gider
        print(f"uyarı: VLM hakemliği başarısız ({exc}); olay verdict'siz gönderiliyor", file=sys.stderr)
