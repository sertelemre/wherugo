"""SQLAlchemy 2.0 models — table/field names per CONTRACTS.md section 3."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    isolation_tier: Mapped[str] = mapped_column(String(32), default="shared")
    # v2 (CONTRACTS section 9): sha256 hex of the tenant API key; never the key itself.
    api_key_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class Store(Base):
    __tablename__ = "store"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    plan_width_m: Mapped[float] = mapped_column(Float)
    plan_height_m: Mapped[float] = mapped_column(Float)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Istanbul")
    # v2 (CONTRACTS section 11): queue alerts are POSTed here when set.
    webhook_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    zones: Mapped[list["Zone"]] = relationship(back_populates="store")


class EdgeDevice(Base):
    __tablename__ = "edge_device"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("store.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    health_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)


class Camera(Base):
    __tablename__ = "camera"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("store.id"), index=True)
    device_id: Mapped[Optional[str]] = mapped_column(ForeignKey("edge_device.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    homography_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    reproj_error_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class Zone(Base):
    __tablename__ = "zone"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("store.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    zone_type: Mapped[str] = mapped_column(String(32))  # entrance|shelf|queue|checkout|fitting_room|other
    polygon_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    store: Mapped["Store"] = relationship(back_populates="zones")


class EventRaw(Base):
    __tablename__ = "event_raw"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    store_id: Mapped[int] = mapped_column(Integer, index=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    seq_no: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(32), index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    payload_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_event_raw_device_seq", "device_id", "seq_no"),)


class TrackPosition(Base):
    __tablename__ = "track_position"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(Integer, index=True)
    camera_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    track_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    x_m: Mapped[float] = mapped_column(Float)
    y_m: Mapped[float] = mapped_column(Float)
    sigma_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    conf: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    __table_args__ = (Index("ix_track_position_store_ts", "store_id", "ts"),)


class ZoneVisit(Base):
    __tablename__ = "zone_visit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(Integer, index=True)
    zone_id: Mapped[int] = mapped_column(Integer, index=True)
    track_id: Mapped[int] = mapped_column(Integer, index=True)
    enter_ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    exit_ts: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    dwell_sec: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    classification: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # dwell|pass_by
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)


class QueueSample(Base):
    __tablename__ = "queue_sample"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(Integer, index=True)
    zone_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    queue_len: Mapped[int] = mapped_column(Integer)
    est_wait_sec: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    joins: Mapped[int] = mapped_column(Integer, default=0)
    abandons: Mapped[int] = mapped_column(Integer, default=0)
    active_checkouts: Mapped[int] = mapped_column(Integer, default=0)


class CoverageGap(Base):
    __tablename__ = "coverage_gap"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(Integer, index=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    gap_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    gap_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    missing_seq_from: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    missing_seq_to: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(32), default="seq_gap")


class PosDaily(Base):
    __tablename__ = "pos_daily"

    store_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    transactions: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)


class Alert(Base):
    """Queue alert (CONTRACTS section 11). DB column for the delivery flag is
    named `delivered_bool` per the contract; the Python attribute is `delivered`."""
    __tablename__ = "alert"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(Integer, index=True)
    zone_id: Mapped[int] = mapped_column(Integer, index=True)
    type: Mapped[str] = mapped_column(String(32))  # queue_length | queue_wait
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    payload_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    delivered: Mapped[bool] = mapped_column("delivered_bool", Boolean, default=False)


class Briefing(Base):
    __tablename__ = "briefing"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(Integer, index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    text_md: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(64), default="mock")
    metric_refs_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
