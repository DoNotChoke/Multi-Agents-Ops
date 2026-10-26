"""Background lifecycle: resolve incidents whose alerts stayed quiet, close old resolved ones."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from opsteam_core.clock import Clock
from opsteam_core.context import Principal, RequestContext
from opsteam_core.errors import OpsteamError
from opsteam_db import tenant_session
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from opsteam_incidents.state import OPEN, transition

PRINCIPAL = Principal(type="system", id="lifecycle")
INTERVAL_S = 30.0
_LOCK_KEY = "opsteam:incident-lifecycle"

log = structlog.get_logger()

# Each alert's latest state: a re-fire starts later; a resolution shares its firing's start.
_ALERTS = text("""
    with latest as (
      select distinct on (e.fingerprint) e.status, e.received_at
      from incident_events ie join events e on e.event_id = ie.event_id
      where ie.incident_id = :id
      order by e.fingerprint, e.starts_at desc, (e.status = 'resolved') desc, e.received_at desc
    )
    select count(*) as alerts, count(*) filter (where status = 'firing') as firing,
           max(received_at) as quiet_since
    from latest
""")


@dataclass(frozen=True)
class Windows:
    stable: timedelta = timedelta(minutes=10)
    close_after: timedelta = timedelta(hours=24)


async def sweep(
    engine: AsyncEngine, system_engine: AsyncEngine, clock: Clock, windows: Windows
) -> int:
    """Settle every candidate incident once and return how many changed; one sweeper at a time."""
    changed = 0
    async with system_engine.connect() as conn:
        locked = await conn.scalar(
            text("select pg_try_advisory_lock(hashtextextended(:k, 0))"), {"k": _LOCK_KEY}
        )
        if not locked:
            return 0
        try:
            # shortcut: re-checks every unclosed incident each sweep; add a due_at index at scale.
            candidates = (
                await conn.execute(text("select * from app_lifecycle_candidates()"))
            ).all()
            for tenant_id, incident_id in candidates:
                ctx = RequestContext(
                    tenant_id=tenant_id, principal=PRINCIPAL, roles=(), scopes=frozenset()
                )
                try:
                    async with tenant_session(engine, ctx) as session:
                        changed += await _settle(session, ctx, incident_id, clock.now(), windows)
                except (OpsteamError, SQLAlchemyError) as exc:
                    log.warning("lifecycle.settle_failed", incident_id=incident_id, error=str(exc))
        finally:
            await conn.execute(
                text("select pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": _LOCK_KEY}
            )
    return changed


async def _settle(
    session: AsyncSession, ctx: RequestContext, incident_id: str, now: datetime, w: Windows
) -> int:
    incident = (
        await session.execute(
            text("select status, resolved_at from incidents where incident_id = :id for update"),
            {"id": incident_id},
        )
    ).one_or_none()
    if incident is None:
        return 0
    if incident.status == "RESOLVED" and incident.resolved_at <= now - w.close_after:
        await transition(session, ctx, incident_id, "CLOSED", "closed_after_quiet_period", now)
        return 1
    if incident.status not in OPEN:
        return 0
    alerts = (await session.execute(_ALERTS, {"id": incident_id})).one()
    if alerts.alerts and not alerts.firing and alerts.quiet_since <= now - w.stable:
        await transition(session, ctx, incident_id, "RESOLVED", "auto_resolved", now)
        return 1
    return 0


async def run(
    engine: AsyncEngine, system_engine: AsyncEngine, clock: Clock, windows: Windows
) -> None:
    """Sweep until cancelled; a database outage only skips a round."""
    while True:
        try:
            await sweep(engine, system_engine, clock, windows)
        except (SQLAlchemyError, OSError) as exc:
            log.warning("lifecycle.unavailable", error=type(exc).__name__)
        await asyncio.sleep(INTERVAL_S)
