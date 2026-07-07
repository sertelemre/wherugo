"""FastAPI app factory: create_all + demo seed on startup, CORS open,
serves ../dashboard/index.html at / when present."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from . import __version__
from .api import router
from .auth import DEMO_TENANT_ID
from .db import Base, create_db_engine, create_session_factory
from .models import Camera, EdgeDevice, Store, Tenant, Zone

DEMO_STORE_ID = 1
DEMO_DEVICE_ID = "edge-1a"

# Zones for the 20x12 m demo fashion store (CONTRACTS section 6:
# entrance, 4 shelves, fitting room, queue, checkout). Fixed ids 1..8 so the
# edge simulator config (deploy/edge-demo.yaml) can reference them stably.
DEMO_ZONES: list[dict] = [
    {"id": 1, "name": "entrance", "zone_type": "entrance", "category": None,
     "polygon": [[8, 0], [12, 0], [12, 2], [8, 2]]},
    {"id": 2, "name": "shelf_women", "zone_type": "shelf", "category": "women",
     "polygon": [[1, 3], [6, 3], [6, 6], [1, 6]]},
    {"id": 3, "name": "shelf_men", "zone_type": "shelf", "category": "men",
     "polygon": [[1, 7], [6, 7], [6, 10], [1, 10]]},
    {"id": 4, "name": "shelf_accessories", "zone_type": "shelf", "category": "accessories",
     "polygon": [[8, 4], [13, 4], [13, 7], [8, 7]]},
    {"id": 5, "name": "shelf_shoes", "zone_type": "shelf", "category": "shoes",
     "polygon": [[14, 3], [19, 3], [19, 6], [14, 6]]},
    {"id": 6, "name": "fitting_room", "zone_type": "fitting_room", "category": None,
     "polygon": [[14, 8], [19, 8], [19, 11], [14, 11]]},
    {"id": 7, "name": "queue", "zone_type": "queue", "category": None,
     "polygon": [[6, 9], [9, 9], [9, 11], [6, 11]]},
    {"id": 8, "name": "checkout", "zone_type": "checkout", "category": None,
     "polygon": [[2, 9], [5, 9], [5, 11], [2, 11]]},
]


def seed_demo(session: Session) -> None:
    """Idempotent demo seed: tenant t_demo + store 1 'Demo Mağaza' (20x12 m) + zones."""
    if session.get(Tenant, DEMO_TENANT_ID) is not None:
        return
    session.add(Tenant(id=DEMO_TENANT_ID, name="Demo Tenant", isolation_tier="shared"))
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
        Base.metadata.create_all(engine)
        with session_factory() as session:
            seed_demo(session)
        yield
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

    dashboard = _dashboard_dir()
    if dashboard.is_dir():
        app.mount("/static", StaticFiles(directory=str(dashboard)), name="static")

    @app.get("/", include_in_schema=False)
    def root():
        index = dashboard / "index.html"
        if index.is_file():
            return FileResponse(str(index))
        return JSONResponse({
            "service": "wherugo-backend",
            "version": __version__,
            "docs": "/docs",
            "api_base": "/v1",
            "note": "dashboard/index.html not found; API-only mode",
        })

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    return app


app = create_app()
