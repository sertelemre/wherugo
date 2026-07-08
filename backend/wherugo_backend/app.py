"""FastAPI app factory: column migrations + create_all + demo seed on startup,
CORS open, serves ../dashboard/index.html at / when present. v2: daily
briefing scheduler task (WHERUGO_BRIEFING_AUTO=0 disables)."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import scheduler, __version__
from .api import router
from .auth import DEMO_TENANT_ID, demo_api_key, hash_api_key
from .db import Base, create_db_engine, create_session_factory
from .models import Camera, EdgeDevice, Store, Tenant, Zone

DEMO_STORE_ID = 1
DEMO_DEVICE_ID = "edge-1a"

# Zones for the 20x12 m demo fashion store (CONTRACTS section 6: entrance,
# 4 shelves, fitting room, queue, checkout). Names and polygons MUST mirror
# deploy/edge-demo.yaml: the simulator generates traffic against its own
# polygons and the dashboard overlays the polygons stored here.
DEMO_ZONES: list[dict] = [
    {"id": 1, "name": "Giriş", "zone_type": "entrance", "category": None,
     "polygon": [[8, 0], [12, 0], [12, 2], [8, 2]]},
    {"id": 2, "name": "Kadın Üst Giyim", "zone_type": "shelf", "category": "kadin",
     "polygon": [[1, 3], [6, 3], [6, 7], [1, 7]]},
    {"id": 3, "name": "Erkek Üst Giyim", "zone_type": "shelf", "category": "erkek",
     "polygon": [[14, 3], [19, 3], [19, 7], [14, 7]]},
    {"id": 4, "name": "Aksesuar", "zone_type": "shelf", "category": "aksesuar",
     "polygon": [[7.5, 3.5], [12.5, 3.5], [12.5, 8], [7.5, 8]]},
    {"id": 5, "name": "Ayakkabı", "zone_type": "shelf", "category": "ayakkabi",
     "polygon": [[1, 8], [6, 8], [6, 11.5], [1, 11.5]]},
    {"id": 6, "name": "Deneme Kabini Önü", "zone_type": "fitting_room", "category": None,
     "polygon": [[14, 8], [17, 8], [17, 11.5], [14, 11.5]]},
    {"id": 7, "name": "Kasa Kuyruğu", "zone_type": "queue", "category": None,
     "polygon": [[7, 9], [12, 9], [12, 10.5], [7, 10.5]]},
    {"id": 8, "name": "Kasa", "zone_type": "checkout", "category": None,
     "polygon": [[7, 10.5], [12, 10.5], [12, 12], [7, 12]]},
]


# v2 columns that create_all will NOT add to a pre-existing table (SQLAlchemy
# create_all only creates missing TABLES). Forward-only, additive migrations;
# the plain "ALTER TABLE ... ADD COLUMN" syntax works on both SQLite and PG.
V2_COLUMNS: list[tuple[str, str, str]] = [
    ("tenant", "api_key_hash", "VARCHAR(64)"),
    ("store", "webhook_url", "VARCHAR(500)"),
]


def run_column_migrations(engine: Engine) -> None:
    """Add missing v2 columns to already-existing tables (no-op on fresh DBs:
    create_all creates those tables with the columns already in place)."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, column, ddl_type in V2_COLUMNS:
        if table not in tables:
            continue  # create_all will create it with the column included
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column in existing:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def seed_demo(session: Session) -> None:
    """Idempotent demo seed: tenant t_demo + store 1 'Demo Mağaza' (20x12 m) + zones.
    Also (re)fills t_demo.api_key_hash from WHERUGO_DEMO_API_KEY so a DB
    migrated from v1 can immediately use POST /v1/auth/token."""
    demo_hash = hash_api_key(demo_api_key())
    existing = session.get(Tenant, DEMO_TENANT_ID)
    if existing is not None:
        if not existing.api_key_hash:  # v1 database migrated in place
            existing.api_key_hash = demo_hash
            session.commit()
        return
    session.add(Tenant(id=DEMO_TENANT_ID, name="Demo Tenant", isolation_tier="shared",
                       api_key_hash=demo_hash))
    session.add(Store(id=DEMO_STORE_ID, tenant_id=DEMO_TENANT_ID, name="Demo Mağaza",
                      plan_width_m=20.0, plan_height_m=12.0, timezone="Europe/Istanbul"))
    session.add(EdgeDevice(id=DEMO_DEVICE_ID, store_id=DEMO_STORE_ID, name="Demo Edge",
                           last_heartbeat=None, health_json=None))
    session.add(Camera(id=1, store_id=DEMO_STORE_ID, device_id=DEMO_DEVICE_ID,
                       name="dome-entrance",
                       homography_json=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                       reproj_error_cm=4.2))
    for z in DEMO_ZONES:
        session.add(Zone(id=z["id"], store_id=DEMO_STORE_ID, name=z["name"],
                         zone_type=z["zone_type"], polygon_json=z["polygon"],
                         category=z["category"]))
    session.commit()


def _dashboard_dir() -> Path:
    override = os.environ.get("WHERUGO_DASHBOARD_DIR")
    if override:
        return Path(override)
    # backend/wherugo_backend/app.py -> repo root -> dashboard/
    return Path(__file__).resolve().parents[2] / "dashboard"


def create_app(db_url: str | None = None) -> FastAPI:
    engine = create_db_engine(db_url)
    session_factory: sessionmaker = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        run_column_migrations(engine)
        Base.metadata.create_all(engine)
        with session_factory() as session:
            seed_demo(session)
        # v2 (CONTRACTS section 14): daily briefing scheduler. Disabled with
        # WHERUGO_BRIEFING_AUTO=0 (tests set this so no background task lingers).
        briefing_task: asyncio.Task | None = None
        if scheduler.briefing_auto_enabled():
            briefing_task = asyncio.create_task(scheduler.briefing_loop(session_factory))
        yield
        if briefing_task is not None:
            briefing_task.cancel()
            try:
                await briefing_task
            except asyncio.CancelledError:
                pass
        engine.dispose()

    app = FastAPI(title="WherUGo Backend", version=__version__, lifespan=lifespan)
    app.state.engine = engine
    app.state.sessionmaker = session_factory

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    dashboard = _dashboard_dir()
    if dashboard.is_dir() and (dashboard / "index.html").is_file():
        # Kök mount EN SON eklenir: /v1 ve /healthz önce eşleşir; index.html
        # varlıkları göreli yoldan (/app.js, /style.css) istediği için kökten
        # servis edilmek zorundadır (/static altına koymak onları 404 yapar).
        app.mount("/", StaticFiles(directory=str(dashboard), html=True), name="dashboard")
    else:
        @app.get("/", include_in_schema=False)
        def root():
            return JSONResponse({
                "service": "wherugo-backend",
                "version": __version__,
                "docs": "/docs",
                "api_base": "/v1",
                "note": "dashboard/index.html not found; API-only mode",
            })

    return app


app = create_app()
