"""Quality badge rules + k-anonymity suppression helpers.

Badge rule (CONTRACTS section 4): coverage gap duration within the query window
> 2% -> yellow, > 10% -> red; no data at all -> red.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import CoverageGap, EventRaw
from .schemas import QualityDetail

K_ANONYMITY = 10

T = TypeVar("T")


def badge_from_gap(gap_sec: float, window_sec: float, has_data: bool) -> str:
    if not has_data or window_sec <= 0:
        return "red"
    pct = 100.0 * gap_sec / window_sec
    if pct > 10.0:
        return "red"
    if pct > 2.0:
        return "yellow"
    return "green"


def gap_overlap_seconds(gaps: Iterable[tuple[datetime | None, datetime | None]],
                        t_from: datetime, t_to: datetime) -> float:
    """Sum of gap-interval overlap with [t_from, t_to]. Open/unknown ends count as 0."""
    total = 0.0
    for gap_start, gap_end in gaps:
        if gap_start is None or gap_end is None:
            continue
        start = max(gap_start, t_from)
        end = min(gap_end, t_to)
        if end > start:
            total += (end - start).total_seconds()
    return total


def quality_context(session: Session, store_id: int, t_from: datetime, t_to: datetime,
                    has_data: bool | None = None) -> tuple[str, QualityDetail]:
    """Compute (badge, detail) for a store over a window.

    has_data defaults to "any raw event in window"; endpoints may override
    (e.g. a metric that found rows can pass has_data=True).
    """
    window_sec = max((t_to - t_from).total_seconds(), 0.0)
    gap_rows = session.execute(
        select(CoverageGap.gap_start, CoverageGap.gap_end)
        .where(CoverageGap.store_id == store_id)
        .where((CoverageGap.gap_start == None) | (CoverageGap.gap_start <= t_to))  # noqa: E711
        .where((CoverageGap.gap_end == None) | (CoverageGap.gap_end >= t_from))  # noqa: E711
    ).all()
    gap_sec = gap_overlap_seconds(gap_rows, t_from, t_to)

    if has_data is None:
        has_data = bool(session.scalar(
            select(func.count()).select_from(EventRaw)
            .where(EventRaw.store_id == store_id,
                   EventRaw.event_time >= t_from,
                   EventRaw.event_time <= t_to)
        ))

    badge = badge_from_gap(gap_sec, window_sec, has_data)
    pct = (100.0 * gap_sec / window_sec) if window_sec > 0 else 0.0
    reason = None
    if not has_data:
        reason = "no_data"
    elif badge != "green":
        reason = "coverage_gap"
    detail = QualityDetail(
        window_sec=window_sec,
        coverage_gap_sec=round(gap_sec, 3),
        coverage_gap_pct=round(pct, 3),
        gap_count=len(gap_rows),
        has_data=bool(has_data),
        reason=reason,
    )
    return badge, detail


def k_suppress(items: Iterable[T], count_of: Callable[[T], int], k: int = K_ANONYMITY) -> tuple[list[T], int]:
    """Drop aggregate rows whose underlying unique-person count is below k.

    Returns (kept_items, suppressed_count)."""
    items = list(items)
    kept = [it for it in items if count_of(it) >= k]
    return kept, len(items) - len(kept)
