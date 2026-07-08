"""Daily automatic briefing scheduler (CONTRACTS section 14).

generate_due_briefings is a pure, synchronously callable function (used by
tests directly); briefing_loop is the asyncio task started from the app
lifespan every CHECK_INTERVAL_SEC seconds unless WHERUGO_BRIEFING_AUTO=0.

Rule: once a store's LOCAL clock passes WHERUGO_BRIEFING_HOUR (default 07),
YESTERDAY's (store-local) briefing is generated if it does not exist yet.
Idempotent: an existing briefing row for (store, yesterday) is never
regenerated. When the wherugo_ai package cannot be imported the run is a
silent no-op.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import insights
from .models import Briefing, Store
from .util import utcnow

DEFAULT_BRIEFING_HOUR = 7
DEFAULT_TIMEZONE = "Europe/Istanbul"
CHECK_INTERVAL_SEC = 60.0

BRIEFING_HOUR_ENV = "WHERUGO_BRIEFING_HOUR"
BRIEFING_AUTO_ENV = "WHERUGO_BRIEFING_AUTO"


def briefing_hour() -> int:
    try:
        return int(os.environ.get(BRIEFING_HOUR_ENV, str(DEFAULT_BRIEFING_HOUR)))
    except ValueError:
        return DEFAULT_BRIEFING_HOUR


def briefing_auto_enabled() -> bool:
    return (os.environ.get(BRIEFING_AUTO_ENV, "1").strip().lower()
            not in ("0", "false", "no", "off"))


def _store_tz(store: Store) -> ZoneInfo:
    try:
        return ZoneInfo(store.timezone or DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def generate_due_briefings(session_factory, now: datetime | None = None) -> int:
    """Generate every due 'yesterday' briefing; returns how many were created.

    `now` is a naive-UTC datetime (defaults to the real clock) so tests can
    freeze time. Safe with multiple workers: the existence check runs right
    before each insert, and duplicates are additionally tolerated by the
    briefing read path (it always serves the first row)."""
    now = now or utcnow()
    ai = insights.load_ai()
    if ai is None:
        return 0  # ai package not importable: silently skip (CONTRACTS section 14)
    hour = briefing_hour()
    generated = 0
    with session_factory() as session:
        stores = session.scalars(select(Store).order_by(Store.id)).all()
        for store in stores:
            tz = _store_tz(store)
            local_now = now.replace(tzinfo=timezone.utc).astimezone(tz)
            if local_now.hour < hour:
                continue  # store-local briefing hour not reached yet
            day = local_now.date() - timedelta(days=1)
            exists = session.scalars(
                select(Briefing)
                .where(Briefing.store_id == store.id, Briefing.date == day)
                .limit(1)
            ).first()
            if exists is not None:
                continue  # idempotent: already briefed
            start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
            t_from = start_local.astimezone(timezone.utc).replace(tzinfo=None)
            t_to = (start_local + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
            bundle = insights.build_metrics_bundle(session, store.id, t_from, t_to)
            try:
                result = ai.briefing.generate(bundle, store.name, day.isoformat(), ai.provider)
            except Exception:
                continue  # provider hiccup: retried on the next tick
            text_md = getattr(result, "text_md", None) or (
                result.get("text_md") if isinstance(result, dict) else "") or ""
            refs = getattr(result, "metric_refs", None) or (
                result.get("metric_refs") if isinstance(result, dict) else []) or []
            session.add(Briefing(store_id=store.id, date=day, text_md=text_md,
                                 provider=ai.provider_name, metric_refs_json=list(refs)))
            session.commit()
            generated += 1
    return generated


async def briefing_loop(session_factory, interval_sec: float = CHECK_INTERVAL_SEC) -> None:
    """Lifespan task: run generate_due_briefings every interval_sec seconds.
    DB/provider work runs in a thread so the event loop never blocks."""
    while True:
        try:
            await asyncio.to_thread(generate_due_briefings, session_factory)
        except Exception:
            pass  # never let a bad tick kill the loop
        await asyncio.sleep(interval_sec)
