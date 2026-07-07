"""Small shared helpers (time parsing, UTC handling)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def utcnow() -> datetime:
    """Naive UTC now — all DB timestamps are stored naive-UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_ts(value: str | datetime | None) -> datetime | None:
    """Parse RFC3339 / ISO8601 into naive UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_window(from_: str | None, to: str | None, default_hours: int = 24) -> tuple[datetime, datetime]:
    """Resolve a (from, to) query window; default = last `default_hours`."""
    t_to = parse_ts(to) or utcnow()
    t_from = parse_ts(from_) or (t_to - timedelta(hours=default_hours))
    return t_from, t_to


def parse_date(value: str | None) -> date | None:
    if value is None or value == "":
        return None
    return date.fromisoformat(str(value)[:10])
