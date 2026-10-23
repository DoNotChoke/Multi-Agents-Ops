"""Engine creation and the tenant-scoped session every query must go through."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from opsteam_core.context import RequestContext
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine


def create_engine(url: str | URL, **kwargs: Any) -> AsyncEngine:
    """Create an async engine for a `postgresql+asyncpg` URL."""
    return create_async_engine(url, pool_pre_ping=True, **kwargs)


async def ping(engine: AsyncEngine) -> None:
    """Raise if the database cannot answer a trivial query; reads no tenant data."""
    async with engine.connect() as conn:
        await conn.execute(text("select 1"))


@asynccontextmanager
async def tenant_session(engine: AsyncEngine, ctx: RequestContext) -> AsyncIterator[AsyncSession]:
    """Open a transaction that RLS limits to ctx's tenant; commits on exit, rolls back on error."""
    if not isinstance(ctx, RequestContext):
        raise TypeError("tenant_session needs an authenticated RequestContext")
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        # is_local=true: the setting dies with the transaction, so pooled connections start clean.
        await session.execute(
            text("select set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": ctx.tenant_id},
        )
        yield session
