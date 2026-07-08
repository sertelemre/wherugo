"""Small shared helpers (time parsing, UTC handling)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException


def utcnow() -> datetime:
    """Naive UTC now — all DB timestamps are stored naive-UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_ts(value: str | datetime | None) -> datetime | None:
    """Parse RFC3339 / ISO8601 into naive UTC datetime.

    Returns None for None input AND for unparseable strings — callers that
    need to distinguish "absent" from "invalid" must check the raw value
    themselves (see _window_ts)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _window_ts(value: str | None, param: str) -> datetime | None:
    """Parse a user-supplied window bound; absent/blank -> None, invalid -> 422."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    ts = parse_ts(value)
    if ts is None:
        raise HTTPException(
            status_code=422,
            detail=f"invalid '{param}' timestamp (RFC3339/ISO 8601 expected)")
    return ts


def parse_window(from_: str | None, to: str | None, default_hours: int = 24) -> tuple[datetime, datetime]:
    """Resolve a (from, to) query window; default = last `default_hours`.

    Malformed values raise HTTPException(422) instead of crashing the endpoint."""
    t_to = _window_ts(to, "to") or utcnow()
    t_from = _window_ts(from_, "from") or (t_to - timedelta(hours=default_hours))
    return t_from, t_to


def parse_date(value: str | None) -> date | None:
    """None/empty -> None; invalid format raises ValueError (callers map to 422)."""
    if value is None or value == "":
        return None
    return date.fromisoformat(str(value)[:10])
