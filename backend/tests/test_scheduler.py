"""Daily briefing scheduler (CONTRACTS section 14): hour gating + idempotency."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select

from wherugo_backend import scheduler
from wherugo_backend.app import seed_demo
from wherugo_backend.db import Base, create_db_engine, create_session_factory
from wherugo_backend.models import Briefing


def _factory():
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    sf = create_session_factory(engine)
    with sf() as s:
        seed_demo(s)  # store 1, timezone Europe/Istanbul (UTC+3)
    return sf


def _briefings(sf):
    with sf() as s:
        return s.scalars(select(Briefing).order_by(Briefing.id)).all()


def test_no_briefing_before_local_hour(monkeypatch):
    monkeypatch.setenv("WHERUGO_BRIEFING_HOUR", "7")
    sf = _factory()
    # 02:30 UTC = 05:30 Istanbul local -> before 07 -> nothing due
    assert scheduler.generate_due_briefings(sf, now=datetime(2026, 7, 8, 2, 30)) == 0
    assert _briefings(sf) == []


def test_briefing_generated_after_hour_and_idempotent(monkeypatch):
    monkeypatch.setenv("WHERUGO_BRIEFING_HOUR", "7")
    sf = _factory()
    # 05:00 UTC = 08:00 Istanbul local -> due; yesterday (local) = 2026-07-07
    now = datetime(2026, 7, 8, 5, 0)
    assert scheduler.generate_due_briefings(sf, now=now) == 1
    rows = _briefings(sf)
    assert len(rows) == 1
    assert rows[0].store_id == 1
    assert rows[0].date == date(2026, 7, 7)
    assert rows[0].text_md  # deterministic mock template, never empty
    assert rows[0].provider

    # second tick with the same clock: idempotent, nothing regenerated
    assert scheduler.generate_due_briefings(sf, now=now) == 0
    assert len(_briefings(sf)) == 1

    # next day: exactly one new briefing (for 2026-07-08)
    assert scheduler.generate_due_briefings(sf, now=datetime(2026, 7, 9, 5, 0)) == 1
    assert {r.date for r in _briefings(sf)} == {date(2026, 7, 7), date(2026, 7, 8)}


def test_briefing_hour_env_is_respected(monkeypatch):
    sf = _factory()
    now = datetime(2026, 7, 8, 5, 0)  # 08:00 local
    monkeypatch.setenv("WHERUGO_BRIEFING_HOUR", "9")
    assert scheduler.generate_due_briefings(sf, now=now) == 0  # 08 < 09
    monkeypatch.setenv("WHERUGO_BRIEFING_HOUR", "8")
    assert scheduler.generate_due_briefings(sf, now=now) == 1  # 08 >= 08


def test_missing_ai_package_is_a_silent_noop(monkeypatch):
    sf = _factory()
    monkeypatch.setattr(scheduler.insights, "load_ai", lambda: None)
    assert scheduler.generate_due_briefings(sf, now=datetime(2026, 7, 8, 12, 0)) == 0
    assert _briefings(sf) == []


def test_briefing_auto_flag_parsing(monkeypatch):
    for off in ("0", "false", "no", "off"):
        monkeypatch.setenv("WHERUGO_BRIEFING_AUTO", off)
        assert scheduler.briefing_auto_enabled() is False
    monkeypatch.setenv("WHERUGO_BRIEFING_AUTO", "1")
    assert scheduler.briefing_auto_enabled() is True
    monkeypatch.delenv("WHERUGO_BRIEFING_AUTO")
    assert scheduler.briefing_auto_enabled() is True  # default on
