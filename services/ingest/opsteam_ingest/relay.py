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

MAX_ATTEMPTS = 10
MAX_BACKOFF_S = 300

# A row waits while an older row of the same tenant and topic is backing off, so each
# tenant's events stay in order without one tenant's failures holding up anyone else.
_CLAIM = text("""
    select o.outbox_id, o.tenant_id, o.topic, o.payload, o.attempts from outbox o
    where o.published_at is null
      and (o.next_attempt_at is null or o.next_attempt_at <= now())
      and not exists (
        select 1 from outbox b
        where b.published_at is null and b.tenant_id = o.tenant_id and b.topic = o.topic
          and b.outbox_id < o.outbox_id and b.next_attempt_at > now()
      )
    order by o.outbox_id
    for update skip locked
    limit :limit
""")
_DONE = text("update outbox set published_at = now() where outbox_id = :id")
_RETRY = text("""
    update outbox set attempts = attempts + 1,
      next_attempt_at = now() + make_interval(secs => :delay)
    where outbox_id = :id
""")
_DEAD = text(
    "update outbox set attempts = attempts + 1, published_at = now() where outbox_id = :id"
)


async def relay_once(engine: AsyncEngine, publisher: EventPublisher, limit: int = BATCH) -> int:
    """Publish up to `limit` rows and return how many; a failing row backs off and retries."""
    published = 0
    blocked: set[tuple[str, str]] = set()
    async with engine.begin() as conn:
        for row in (await conn.execute(_CLAIM, {"limit": limit})).all():
            if (row.tenant_id, row.topic) in blocked:
                continue
            ctx = RequestContext(
                tenant_id=row.tenant_id, principal=PRINCIPAL, roles=(), scopes=frozenset()
            )
            try:
                await publisher.publish(ctx, row.topic, row.payload)
            except (OpsteamError, SQLAlchemyError, OSError) as exc:
                blocked.add((row.tenant_id, row.topic))
                if row.attempts + 1 >= MAX_ATTEMPTS:
                    log.error("outbox.dead_letter", outbox_id=row.outbox_id, topic=row.topic)
                    await conn.execute(_DEAD, {"id": row.outbox_id})
                    continue
                delay = min(BACKOFF_S * 2**row.attempts, MAX_BACKOFF_S)
                log.warning("outbox.publish_failed", outbox_id=row.outbox_id, error=str(exc))
                await conn.execute(_RETRY, {"id": row.outbox_id, "delay": delay})
                continue
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
