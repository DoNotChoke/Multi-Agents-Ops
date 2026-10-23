import pytest
from fastapi.testclient import TestClient
from opsteam_api.main import create_app
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


def test_readyz_ok_with_database(app_engine: AsyncEngine) -> None:
    with TestClient(create_app(engine=app_engine)) as client:
        assert client.get("/readyz").json() == {"status": "ok"}
