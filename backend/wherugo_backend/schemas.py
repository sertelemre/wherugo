"""Pydantic request/response models (CONTRACTS.md sections 2 and 4)."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

MAX_BATCH_SIZE = 500


# --- ingest -----------------------------------------------------------------

class IngestRequest(BaseModel):
    # Events are kept schemaless dicts on purpose: the envelope may arrive
    # nested (docs/02 section 6 style) or flat (CONTRACTS section 2 style);
    # ingest.py normalizes both. Batch size is enforced in the endpoint.
    events: list[dict[str, Any]] = Field(default_factory=list)


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    gap_detected: bool
    # Events dropped individually (malformed envelope/fields, foreign-tenant
    # store, out-of-range dwell). Additive to the CONTRACTS section 2 trio.
    rejected: int = 0


# --- quality ----------------------------------------------------------------

class QualityDetail(BaseModel):
    window_sec: float
    coverage_gap_sec: float
    coverage_gap_pct: float
    gap_count: int
    has_data: bool
    reason: Optional[str] = None


# --- stores -----------------------------------------------------------------

class ZoneOut(BaseModel):
    id: int
    store_id: int
    name: str
    zone_type: str
    polygon: Optional[Any] = None
    category: Optional[str] = None


class StoreOut(BaseModel):
    id: int
    tenant_id: str
    name: str
    plan_width_m: float
    plan_height_m: float
    timezone: str
    # v2: alert webhook target (CONTRACTS section 11); null when unset.
    webhook_url: Optional[str] = None
    zones: list[ZoneOut] = Field(default_factory=list)


# --- auth v2 (CONTRACTS section 9) --------------------------------------------

class TokenRequest(BaseModel):
    api_key: str


class TokenResponse(BaseModel):
    token: str
    expires_in: int


# --- management API (CONTRACTS section 10) -------------------------------------

ZONE_TYPE_PATTERN = "^(entrance|shelf|queue|checkout|fitting_room|other)$"


class StoreCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    plan_width_m: float = Field(gt=0)
    plan_height_m: float = Field(gt=0)
    timezone: str = "Europe/Istanbul"
    webhook_url: Optional[str] = None


class StoreUpdate(BaseModel):
    """Partial update: only fields present in the request body are applied."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    plan_width_m: Optional[float] = Field(default=None, gt=0)
    plan_height_m: Optional[float] = Field(default=None, gt=0)
    timezone: Optional[str] = None
    webhook_url: Optional[str] = None


class ZoneCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    zone_type: str = Field(pattern=ZONE_TYPE_PATTERN)
    polygon: list[list[float]] = Field(min_length=3)
    category: Optional[str] = None


class ZoneUpdate(BaseModel):
    """Partial update: only fields present in the request body are applied."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    zone_type: Optional[str] = Field(default=None, pattern=ZONE_TYPE_PATTERN)
    polygon: Optional[list[list[float]]] = Field(default=None, min_length=3)
    category: Optional[str] = None


class DeviceCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = ""


# --- alerts (CONTRACTS section 11) ----------------------------------------------

class AlertOut(BaseModel):
    id: int
    store_id: int
    zone_id: int
    type: str  # queue_length | queue_wait
    ts: str
    queue_len: Optional[int] = None
    est_wait_sec: Optional[float] = None
    delivered: bool


# --- metrics ----------------------------------------------------------------

class SeriesPoint(BaseModel):
    ts: str
    value: float


class MetricsResponse(BaseModel):
    metric: str
    granularity: str
    series: list[SeriesPoint]
    # None when the metric cannot be rated for the window (e.g. conversion
    # over a window with no fully covered store-local day).
    total: Optional[float] = None
    quality_badge: str
    quality_detail: QualityDetail


class DwellResponse(BaseModel):
    zone_id: int
    zone_name: str
    stats: dict[str, Optional[float]]
    visits: int
    draw_rate: Optional[float]
    # True when stats/draw_rate were hidden by k<10 anonymity suppression.
    suppressed: bool = False
    quality_badge: str
    quality_detail: QualityDetail


class HeatmapCell(BaseModel):
    x: float
    y: float
    value: float


class HeatmapResponse(BaseModel):
    kind: str
    cell_m: float
    cells: list[HeatmapCell]
    k_suppressed: int
    quality_badge: str
    quality_detail: QualityDetail


class FirstDestinationItem(BaseModel):
    zone_id: int
    zone_name: str
    count: int


class FirstDestinationResponse(BaseModel):
    distribution: list[FirstDestinationItem]
    total_tracks: int
    k_suppressed: int
    quality_badge: str
    quality_detail: QualityDetail


class TransitionItem(BaseModel):
    from_zone_id: int
    from_zone: str
    to_zone_id: int
    to_zone: str
    count: int


class TransitionsResponse(BaseModel):
    matrix: list[TransitionItem]
    k_suppressed: int
    quality_badge: str
    quality_detail: QualityDetail


class QueueSampleOut(BaseModel):
    ts: str
    queue_len: int
    est_wait_sec: Optional[float]
    joins: int
    abandons: int
    active_checkouts: int


class QueueLiveResponse(BaseModel):
    zone_id: int
    latest: Optional[QueueSampleOut]
    series: list[QueueSampleOut]
    alert: bool
    quality_badge: str
    quality_detail: QualityDetail


class CoverageGapOut(BaseModel):
    id: int
    device_id: str
    gap_start: Optional[str]
    gap_end: Optional[str]
    missing_seq_from: Optional[int]
    missing_seq_to: Optional[int]
    reason: str
    duration_sec: float


class CoverageGapsResponse(BaseModel):
    gaps: list[CoverageGapOut]
    total_minutes: float
    quality_badge: str
    quality_detail: QualityDetail


class FunnelStep(BaseModel):
    name: str
    value: float


class FunnelResponse(BaseModel):
    steps: list[FunnelStep]
    quality_badge: str
    quality_detail: QualityDetail


class PosImportResponse(BaseModel):
    imported: int
    # Number of unparseable CSV rows that were skipped; omitted (None) when 0
    # so the historical {"imported": n} shape is preserved.
    skipped: Optional[int] = None


# --- ai ---------------------------------------------------------------------

class BriefingResponse(BaseModel):
    store_id: int
    date: str
    text_md: str
    provider: str
    metric_refs: list[str] = Field(default_factory=list)


class AssistantRequest(BaseModel):
    question: str


class AssistantResponse(BaseModel):
    answer_md: str
    metrics_used: list[str] = Field(default_factory=list)


# --- admin ------------------------------------------------------------------

class DeviceHealthResponse(BaseModel):
    id: str
    store_id: int
    name: str
    last_heartbeat: Optional[str]
    online: bool
    health: Optional[Any] = None
