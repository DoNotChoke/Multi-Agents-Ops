"""Outbox relay: hands committed outbox rows to the publisher, oldest first, at least once."""

import asyncio

import structlog
from opsteam_core.context import Principal, RequestContext
from opsteam_core.errors import OpsteamError
from opsteam_core.events import EventPublisher
from opsteam_obs.metrics import OUTBOX_LAG, OUTBOX_UNPUBLISHED
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

PRINCIPAL = Principal(type="system", id="relay")
BATCH = 100
IDLE_S = 0.5
BACKOFF_S = 5.0

log = structlog.get_logger()

_CLAIM = text("""
    select outbox_id, tenant_id, topic, payload from outbox
    where published_at is null
    order by outbox_id
    for update skip locked
    limit :limit
""")
_DONE = text("update outbox set published_at = now() where outbox_id = :id")


async def relay_once(engine: AsyncEngine, publisher: EventPublisher, limit: int = BATCH) -> int:
    """Publish up to `limit` rows and return how many; stops at the first failing row."""
    published = 0
    async with engine.begin() as conn:
        for row in (await conn.execute(_CLAIM, {"limit": limit})).all():
            ctx = RequestContext(
                tenant_id=row.tenant_id, principal=PRINCIPAL, roles=(), scopes=frozenset()
            )
            try:
                await publisher.publish(ctx, row.topic, row.payload)
            except (OpsteamError, SQLAlchemyError, OSError) as exc:
                # Later rows wait so a tenant's events are never handled out of order.
                # shortcut: a row that always fails blocks the queue; add attempts and a
                # dead-letter state once consumers have real failure modes (T-2.3).
                log.warning("outbox.publish_failed", outbox_id=row.outbox_id, error=str(exc))
                break
            await conn.execute(_DONE, {"id": row.outbox_id})
            published += 1
    return published


async def outbox_backlog(engine: AsyncEngine) -> tuple[int, float]:
    """Return the unpublished row count and the age in seconds of the oldest one."""
    async with engine.connect() as conn:
        count, age = (
            await conn.execute(
                text(
                    "select count(*), coalesce(extract(epoch from now() - min(created_at)), 0)"
                    " from outbox where published_at is null"
                )
            )
        ).one()
    return int(count), float(age)


async def run(engine: AsyncEngine, publisher: EventPublisher) -> None:
    """Relay until cancelled; a database outage backs off instead of spinning."""
    while True:
        try:
            published = await relay_once(engine, publisher)
            count, age = await outbox_backlog(engine)
            OUTBOX_UNPUBLISHED.set(count)
            OUTBOX_LAG.set(age)
        except (SQLAlchemyError, OSError) as exc:
            log.warning("outbox.relay_unavailable", error=type(exc).__name__)
            await asyncio.sleep(BACKOFF_S)
            continue
        if published < BATCH:
            await asyncio.sleep(IDLE_S)
