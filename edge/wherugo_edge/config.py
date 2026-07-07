"""YAML config yükleme + doğrulama (örnek: deploy/edge-demo.yaml)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .homography import Homography

ZONE_TYPES = {"entrance", "shelf", "queue", "checkout", "fitting_room", "other"}

DEFAULT_HOURLY_CURVE = [
    0.30, 0.30, 0.30, 0.30, 0.30, 0.35, 0.40, 0.50, 0.60, 0.75, 0.90, 1.00,
    1.00, 0.95, 0.85, 0.80, 0.85, 0.95, 1.00, 0.90, 0.70, 0.55, 0.45, 0.35,
]


class ConfigError(ValueError):
    """Config doğrulama hatası."""


@dataclass
class ZoneDef:
    id: int
    name: str
    zone_type: str
    polygon: list[tuple[float, float]]
    category: Optional[str] = None


@dataclass
class CameraDef:
    id: int
    name: str
    homography: Homography


@dataclass
class StoreInfo:
    name: str = "store"
    plan_width_m: float = 20.0
    plan_height_m: float = 12.0
    timezone: str = "UTC"


@dataclass
class EngineParams:
    hysteresis_samples: int = 2
    dwell_threshold_sec: float = 5.0
    interaction_dwell_sec: float = 8.0
    queue_interval_sec: float = 30.0
    service_time_sec: float = 45.0


@dataclass
class SimParams:
    staff_ratio: float = 0.1
    arrivals_per_hour: float = 120.0
    hourly_curve: list[float] = field(default_factory=lambda: list(DEFAULT_HOURLY_CURVE))
    walk_speed_mps: tuple[float, float] = (0.9, 1.5)
    buy_probability: float = 0.35
    fitting_room_probability: float = 0.25
    pass_by_probability: float = 0.30
    checkout_count: int = 2


@dataclass
class PublisherParams:
    spool_path: str = "wherugo-edge-spool.sqlite"
    batch_size: int = 200
    flush_interval_sec: float = 2.0


@dataclass
class EdgeConfig:
    tenant_id: str
    store_id: int
    device_id: str
    backend_url: str
    auth_token: str = "demo"
    store: StoreInfo = field(default_factory=StoreInfo)
    cameras: list[CameraDef] = field(default_factory=list)
    zones: list[ZoneDef] = field(default_factory=list)
    engine: EngineParams = field(default_factory=EngineParams)
    simulation: SimParams = field(default_factory=SimParams)
    publisher: PublisherParams = field(default_factory=PublisherParams)


def _require(data: dict, key: str, ctx: str) -> Any:
    if key not in data or data[key] is None:
        raise ConfigError(f"{ctx}: zorunlu alan eksik: '{key}'")
    return data[key]


def _parse_polygon(raw: Any, ctx: str) -> list[tuple[float, float]]:
    if not isinstance(raw, list) or len(raw) < 3:
        raise ConfigError(f"{ctx}: polygon en az 3 nokta olmalı")
    pts: list[tuple[float, float]] = []
    for i, p in enumerate(raw):
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ConfigError(f"{ctx}: polygon[{i}] [x, y] çifti olmalı")
        try:
            pts.append((float(p[0]), float(p[1])))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{ctx}: polygon[{i}] sayısal olmalı: {exc}") from exc
    return pts


def _parse_zone(raw: dict, idx: int) -> ZoneDef:
    ctx = f"zones[{idx}]"
    zid = int(_require(raw, "id", ctx))
    ztype = str(_require(raw, "zone_type", ctx))
    if ztype not in ZONE_TYPES:
        raise ConfigError(f"{ctx}: geçersiz zone_type '{ztype}' (izinli: {sorted(ZONE_TYPES)})")
    return ZoneDef(
        id=zid,
        name=str(raw.get("name", f"zone-{zid}")),
        zone_type=ztype,
        polygon=_parse_polygon(_require(raw, "polygon", ctx), ctx),
        category=raw.get("category"),
    )


def _parse_camera(raw: dict, idx: int) -> CameraDef:
    ctx = f"cameras[{idx}]"
    cid = int(_require(raw, "id", ctx))
    hraw = raw.get("homography")
    try:
        homo = Homography(hraw) if hraw is not None else Homography.identity()
    except ValueError as exc:
        raise ConfigError(f"{ctx}: {exc}") from exc
    return CameraDef(id=cid, name=str(raw.get("name", f"cam-{cid}")), homography=homo)


def _parse_sim(raw: dict) -> SimParams:
    sp = SimParams()
    if "staff_ratio" in raw:
        sp.staff_ratio = float(raw["staff_ratio"])
        if not 0.0 <= sp.staff_ratio <= 1.0:
            raise ConfigError(f"simulation.staff_ratio 0-1 arasında olmalı: {sp.staff_ratio}")
    if "arrivals_per_hour" in raw:
        sp.arrivals_per_hour = float(raw["arrivals_per_hour"])
        if sp.arrivals_per_hour < 0:
            raise ConfigError("simulation.arrivals_per_hour negatif olamaz")
    if "hourly_curve" in raw:
        curve = [float(v) for v in raw["hourly_curve"]]
        if len(curve) != 24:
            raise ConfigError(f"simulation.hourly_curve 24 değer olmalı ({len(curve)} verildi)")
        sp.hourly_curve = curve
    if "walk_speed_mps" in raw:
        lo, hi = (float(v) for v in raw["walk_speed_mps"])
        if not 0 < lo <= hi:
            raise ConfigError("simulation.walk_speed_mps [min, max] pozitif olmalı")
        sp.walk_speed_mps = (lo, hi)
    for key in ("buy_probability", "fitting_room_probability", "pass_by_probability"):
        if key in raw:
            val = float(raw[key])
            if not 0.0 <= val <= 1.0:
                raise ConfigError(f"simulation.{key} 0-1 arasında olmalı")
            setattr(sp, key, val)
    if "checkout_count" in raw:
        sp.checkout_count = int(raw["checkout_count"])
        if sp.checkout_count < 1:
            raise ConfigError("simulation.checkout_count >= 1 olmalı")
    return sp


def config_from_dict(data: dict) -> EdgeConfig:
    if not isinstance(data, dict):
        raise ConfigError("config bir YAML eşlemesi (mapping) olmalı")

    cfg = EdgeConfig(
        tenant_id=str(_require(data, "tenant_id", "config")),
        store_id=int(_require(data, "store_id", "config")),
        device_id=str(_require(data, "device_id", "config")),
        backend_url=str(_require(data, "backend_url", "config")).rstrip("/"),
        auth_token=str(data.get("auth_token", "demo")),
    )

    store_raw = data.get("store", {}) or {}
    cfg.store = StoreInfo(
        name=str(store_raw.get("name", "store")),
        plan_width_m=float(store_raw.get("plan_width_m", 20.0)),
        plan_height_m=float(store_raw.get("plan_height_m", 12.0)),
        timezone=str(store_raw.get("timezone", "UTC")),
    )

    cfg.cameras = [_parse_camera(c, i) for i, c in enumerate(data.get("cameras", []) or [])]
    if not cfg.cameras:
        raise ConfigError("en az bir kamera tanımı gerekli (cameras)")

    cfg.zones = [_parse_zone(z, i) for i, z in enumerate(data.get("zones", []) or [])]
    if not cfg.zones:
        raise ConfigError("en az bir zone tanımı gerekli (zones)")
    zone_ids = [z.id for z in cfg.zones]
    if len(zone_ids) != len(set(zone_ids)):
        raise ConfigError("zone id'leri benzersiz olmalı")

    eng_raw = data.get("zone_engine", {}) or {}
    cfg.engine = EngineParams(
        hysteresis_samples=int(eng_raw.get("hysteresis_samples", 2)),
        dwell_threshold_sec=float(eng_raw.get("dwell_threshold_sec", 5.0)),
        interaction_dwell_sec=float(eng_raw.get("interaction_dwell_sec", 8.0)),
        queue_interval_sec=float(eng_raw.get("queue_interval_sec", 30.0)),
        service_time_sec=float(eng_raw.get("service_time_sec", 45.0)),
    )
    if cfg.engine.hysteresis_samples < 1:
        raise ConfigError("zone_engine.hysteresis_samples >= 1 olmalı")

    cfg.simulation = _parse_sim(data.get("simulation", {}) or {})

    pub_raw = data.get("publisher", {}) or {}
    cfg.publisher = PublisherParams(
        spool_path=str(pub_raw.get("spool_path", "wherugo-edge-spool.sqlite")),
        batch_size=min(500, int(pub_raw.get("batch_size", 200))),
        flush_interval_sec=float(pub_raw.get("flush_interval_sec", 2.0)),
    )
    return cfg


def load_config(path: str | Path) -> EdgeConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config dosyası bulunamadı: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return config_from_dict(data)
