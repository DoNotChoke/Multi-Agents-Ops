import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from opsteam_db import create_engine
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine

ALEMBIC_INI = Path(__file__).parents[1] / "packages" / "db" / "alembic.ini"


def _url(role: str, env: str, database: str) -> URL:
    password = os.environ.get(env)
    if not password:
        pytest.fail(f"{env} is not set; run `make test-integration` after `make dev-up`")
    host = os.environ.get("OPSTEAM_TEST_DB_HOST", "127.0.0.1")
    return URL.create("postgresql+asyncpg", role, password, host, 5432, database)


def _alembic_config(url: URL) -> Config:
    cfg = Config(ALEMBIC_INI)
    cfg.attributes["url"] = url.render_as_string(hide_password=False)
    return cfg


@pytest.fixture(scope="session")
def test_db() -> Iterator[str]:
    """Create a throwaway database owned by the migrator and drop it after the session."""
    name = f"opsteam_test_{uuid.uuid4().hex[:12]}"
    admin = _url("opsteam_migrator", "OPSTEAM_MIGRATOR_DB_PASSWORD", "opsteam")

    async def run(sql: str) -> None:
        engine = create_engine(admin, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.execute(text(sql))
        await engine.dispose()

    asyncio.run(run(f'CREATE DATABASE "{name}"'))
    try:
        yield name
    finally:
        asyncio.run(run(f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def migrator_url(test_db: str) -> URL:
    return _url("opsteam_migrator", "OPSTEAM_MIGRATOR_DB_PASSWORD", test_db)


@pytest.fixture(scope="session")
def migrated(migrator_url: URL) -> URL:
    command.upgrade(_alembic_config(migrator_url), "head")
    return migrator_url


@pytest.fixture
async def app_engine(migrated: URL, test_db: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(_url("opsteam_app", "OPSTEAM_APP_DB_PASSWORD", test_db))
    yield engine
    await engine.dispose()


@pytest.fixture
def alembic_cfg(migrated: URL) -> Config:
    return _alembic_config(migrated)
