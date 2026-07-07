import pytest
from fastapi.testclient import TestClient

from wherugo_backend.app import create_app


@pytest.fixture()
def client():
    # Fresh in-memory DB per test (StaticPool keeps the single connection alive).
    app = create_app(db_url="sqlite://")
    with TestClient(app) as c:
        yield c
