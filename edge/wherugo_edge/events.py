"""Olay sözleşmesi: zarf + 5 olay tipi (CONTRACTS §2, docs/02 §6, schema_version=3).

Gizlilik değişmezi: bu modüldeki hiçbir tip piksel/frame taşıyamaz.
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar, Optional

SCHEMA_VERSION = 3

EVENT_TYPES = (
    "track_update",
    "zone_enter",
    "zone_exit",
    "interaction_detected",
    "queue_measurement",
)


def rfc3339(dt: datetime) -> str:
    """UTC RFC3339 ('Z' sonekli, milisaniye çözünürlük)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Quality:
    """Her olayda zorunlu kalite alt nesnesi (CONTRACTS §2)."""

    conf: float
    coverage_ok: bool = True
    occlusion_ratio: Optional[float] = None
    id_switch_risk: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"conf": round(float(self.conf), 3)}
        if self.occlusion_ratio is not None:
            d["occlusion_ratio"] = round(float(self.occlusion_ratio), 3)
        if self.id_switch_risk is not None:
            d["id_switch_risk"] = round(float(self.id_switch_risk), 3)
        d["coverage_ok"] = bool(self.coverage_ok)
        return d


@dataclass
class TrackUpdate:
    TYPE: ClassVar[str] = "track_update"

    event_time: datetime
    camera_id: int
    track_id: int
    x_m: float
    y_m: float
    sigma_cm: float
    is_staff: bool
    quality: Quality

    def payload(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "camera_id": int(self.camera_id),
            "track_id": int(self.track_id),
            "pos": {
                "x_m": round(float(self.x_m), 2),
                "y_m": round(float(self.y_m), 2),
                "sigma_cm": round(float(self.sigma_cm), 1),
            },
            "quality": self.quality.to_dict(),
            "is_staff": bool(self.is_staff),
        }


@dataclass
class ZoneEnter:
    TYPE: ClassVar[str] = "zone_enter"

    event_time: datetime
    zone_id: int
    zone_type: str
    track_id: int
    entry_edge: str
    quality: Quality

    def payload(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "zone_id": int(self.zone_id),
            "zone_type": self.zone_type,
            "track_id": int(self.track_id),
            "entry_edge": self.entry_edge,
            "quality": self.quality.to_dict(),
        }


@dataclass
class ZoneExit:
    TYPE: ClassVar[str] = "zone_exit"

    event_time: datetime
    zone_id: int
    track_id: int
    dwell_sec: float
    classification: str  # dwell | pass_by
    quality: Quality

    def payload(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "zone_id": int(self.zone_id),
            "track_id": int(self.track_id),
            "dwell_sec": round(float(self.dwell_sec), 1),
            "classification": self.classification,
            "quality": self.quality.to_dict(),
        }


@dataclass
class InteractionDetected:
    TYPE: ClassVar[str] = "interaction_detected"

    event_time: datetime
    zone_id: int
    track_id: int
    duration_sec: float
    quality: Quality
    interaction: str = "interaction_candidate"  # Faz 2: pickup | putback (rezerve)
    clip_ref: Optional[str] = None

    def payload(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": self.TYPE,
            "zone_id": int(self.zone_id),
            "track_id": int(self.track_id),
            "interaction": self.interaction,
            "duration_sec": round(float(self.duration_sec), 1),
            "quality": self.quality.to_dict(),
        }
        if self.clip_ref is not None:
            d["clip_ref"] = self.clip_ref
        return d


@dataclass
class QueueMeasurement:
    TYPE: ClassVar[str] = "queue_measurement"

    event_time: datetime
    zone_id: int
    queue_len: int
    est_wait_sec: int
    joins_since_last: int
    abandons_since_last: int
    active_checkouts: int
    quality: Quality
    zone_type: str = "queue"

    def payload(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "zone_id": int(self.zone_id),
            "zone_type": self.zone_type,
            "queue_len": int(self.queue_len),
            "est_wait_sec": int(self.est_wait_sec),
            "joins_since_last": int(self.joins_since_last),
            "abandons_since_last": int(self.abandons_since_last),
            "active_checkouts": int(self.active_checkouts),
            "quality": self.quality.to_dict(),
        }


Event = TrackUpdate | ZoneEnter | ZoneExit | InteractionDetected | QueueMeasurement

EVENT_CLASSES = (TrackUpdate, ZoneEnter, ZoneExit, InteractionDetected, QueueMeasurement)


@dataclass
class Envelope:
    """Her mesajda zorunlu zarf (CONTRACTS §2)."""

    tenant_id: str
    store_id: int
    device_id: str
    seq_no: int
    event_id: str
    event_time: str  # RFC3339
    ingest_time: Optional[str] = None  # backend doldurur
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "store_id": int(self.store_id),
            "device_id": self.device_id,
            "schema_version": int(self.schema_version),
            "seq_no": int(self.seq_no),
            "event_id": self.event_id,
            "event_time": self.event_time,
            "ingest_time": self.ingest_time,
        }


class Enveloper:
    """Olayları zarflar: cihaz başına monoton seq_no + uuid4 event_id.

    rng verilirse event_id'ler seed-deterministik üretilir (dry-run/test).
    """

    def __init__(
        self,
        tenant_id: str,
        store_id: int,
        device_id: str,
        *,
        next_seq: int = 1,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.store_id = store_id
        self.device_id = device_id
        self._next_seq = int(next_seq)
        self._rng = rng

    @property
    def next_seq(self) -> int:
        return self._next_seq

    def _new_event_id(self) -> str:
        if self._rng is not None:
            return str(uuid.UUID(int=self._rng.getrandbits(128), version=4))
        return str(uuid.uuid4())

    def wrap(self, event: Event) -> dict[str, Any]:
        env = Envelope(
            tenant_id=self.tenant_id,
            store_id=self.store_id,
            device_id=self.device_id,
            seq_no=self._next_seq,
            event_id=self._new_event_id(),
            event_time=rfc3339(event.event_time),
        )
        self._next_seq += 1
        return {"envelope": env.to_dict(), **event.payload()}
