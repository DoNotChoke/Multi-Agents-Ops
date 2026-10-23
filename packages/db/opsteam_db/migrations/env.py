"""Alembic environment: runs migrations online over asyncpg as the migrator role."""

import asyncio
import os

from alembic import context
from opsteam_db.session import create_engine
from sqlalchemy import Connection


def _run(connection: Connection) -> None:
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def _main() -> None:
    url = context.config.attributes.get("url") or os.environ["OPSTEAM_MIGRATOR_DATABASE_URL"]
    engine = create_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


asyncio.run(_main())
