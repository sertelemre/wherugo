import os

import pytest
from fastapi.testclient import TestClient

# Tests must never start the background briefing loop: it would keep a task
# alive past the TestClient lifespan and slow the suite down (CONTRACTS
# section 14 — WHERUGO_BRIEFING_AUTO=0 disables it).
os.environ.setdefault("WHERUGO_BRIEFING_AUTO", "0")

from wherugo_backend.app import create_app  # noqa: E402


@pytest.fixture()
def client():
    # Fresh in-memory DB per test (StaticPool keeps the single connection alive).
    app = create_app(db_url="sqlite://")
    with TestClient(app) as c:
        yield c
