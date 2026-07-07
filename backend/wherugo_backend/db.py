"""Engine/session factories. Default DB: sqlite:///wherugo.db (override: WHERUGO_DB_URL)."""
from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

DEFAULT_DB_URL = "sqlite:///wherugo.db"


class Base(DeclarativeBase):
    pass


def get_db_url() -> str:
    return os.environ.get("WHERUGO_DB_URL") or os.environ.get("DATABASE_URL") or DEFAULT_DB_URL


def create_db_engine(db_url: str | None = None) -> Engine:
    url = db_url or get_db_url()
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        # In-memory sqlite must share one connection across threads/sessions.
        if url in ("sqlite://", "sqlite:///:memory:") or ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    return create_engine(url, future=True, **kwargs)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
