"""Startup column migrations: v1 SQLite file gains the v2 columns in place."""
from __future__ import annotations

from sqlalchemy import inspect, text

from wherugo_backend.app import run_column_migrations, seed_demo
from wherugo_backend.auth import hash_api_key
from wherugo_backend.db import Base, create_db_engine, create_session_factory
from wherugo_backend.models import Store, Tenant


def _v1_engine():
    """Simulate a database created by the v1 schema (no api_key_hash /
    webhook_url columns)."""
    engine = create_db_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE tenant (id VARCHAR(64) NOT NULL PRIMARY KEY, "
            "name VARCHAR(200) NOT NULL, isolation_tier VARCHAR(32) NOT NULL)"))
        conn.execute(text(
            "CREATE TABLE store (id INTEGER NOT NULL PRIMARY KEY, "
            "tenant_id VARCHAR(64) NOT NULL, name VARCHAR(200) NOT NULL, "
            "plan_width_m FLOAT NOT NULL, plan_height_m FLOAT NOT NULL, "
            "timezone VARCHAR(64) NOT NULL)"))
        conn.execute(text(
            "INSERT INTO tenant (id, name, isolation_tier) VALUES ('t_demo', 'Demo', 'shared')"))
        conn.execute(text(
            "INSERT INTO store (id, tenant_id, name, plan_width_m, plan_height_m, timezone) "
            "VALUES (1, 't_demo', 'Demo Mağaza', 20.0, 12.0, 'Europe/Istanbul')"))
    return engine


def test_migration_adds_v2_columns_and_is_idempotent():
    engine = _v1_engine()
    run_column_migrations(engine)
    cols_tenant = {c["name"] for c in inspect(engine).get_columns("tenant")}
    cols_store = {c["name"] for c in inspect(engine).get_columns("store")}
    assert "api_key_hash" in cols_tenant
    assert "webhook_url" in cols_store

    run_column_migrations(engine)  # second run: no error, no change
    assert {c["name"] for c in inspect(engine).get_columns("tenant")} == cols_tenant


def test_startup_sequence_on_v1_database_backfills_demo_key():
    """migrate -> create_all -> seed: existing rows survive, new columns are
    usable, and t_demo gets its api_key_hash backfilled."""
    engine = _v1_engine()
    run_column_migrations(engine)
    Base.metadata.create_all(engine)  # creates the NEW tables (alert, ...)
    assert "alert" in inspect(engine).get_table_names()

    sf = create_session_factory(engine)
    with sf() as s:
        seed_demo(s)
        tenant = s.get(Tenant, "t_demo")
        assert tenant.name == "Demo"  # pre-existing row untouched
        assert tenant.api_key_hash == hash_api_key("demo-api-key")
        store = s.get(Store, 1)
        assert store.webhook_url is None
        store.webhook_url = "http://hook.local/x"  # column is writable
        s.commit()


def test_migration_noop_on_fresh_database():
    engine = create_db_engine("sqlite://")
    run_column_migrations(engine)  # no tables yet: nothing to do, no crash
    Base.metadata.create_all(engine)
    run_column_migrations(engine)  # all columns already present
    cols = {c["name"] for c in inspect(engine).get_columns("tenant")}
    assert "api_key_hash" in cols
