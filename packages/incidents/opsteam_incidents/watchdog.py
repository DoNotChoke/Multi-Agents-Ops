"""Dead man's switch: an Alertmanager whose Watchdog stops arriving opens `visibility_lost`."""

import asyncio
from datetime import datetime, timedelta
from typing import Any

import structlog
from opsteam_core.clock import Clock
from opsteam_core.context import Principal, RequestContext
from opsteam_core.errors import OpsteamError
from opsteam_db import tenant_session
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from opsteam_incidents.manager import open_incident
from opsteam_incidents.state import transition

PRINCIPAL = Principal(type="system", id="watchdog")
INTERVAL_S = 30.0
MISSED = 3
_LOCK_KEY = "opsteam:watchdog"

log = structlog.get_logger()

_OPEN = text("""
    select incident_id from incidents
    where root_fingerprint = :fp and status not in ('RESOLVED', 'CLOSED')
    order by opened_at desc limit 1
""")


async def sweep(engine: AsyncEngine, system_engine: AsyncEngine, clock: Clock) -> int:
    """Check every integration that has sent a Watchdog once; return how many incidents changed."""
    changed = 0
    async with system_engine.connect() as conn:
        locked = await conn.scalar(
            text("select pg_try_advisory_lock(hashtextextended(:k, 0))"), {"k": _LOCK_KEY}
        )
        if not locked:
            return 0
        try:
            # shortcut: a deleted integration leaves its open incident to be resolved by hand;
            # sweep open watchdog incidents too if that turns out to happen.
            candidates = (await conn.execute(text("select * from app_watchdog_candidates()"))).all()
            for row in candidates:
                ctx = RequestContext(
                    tenant_id=row.tenant_id, principal=PRINCIPAL, roles=(), scopes=frozenset()
                )
                try:
                    async with tenant_session(engine, ctx) as session:
                        changed += await _check(session, ctx, row, clock.now())
                except (OpsteamError, SQLAlchemyError) as exc:
                    log.warning(
                        "watchdog.check_failed", integration_id=row.integration_id, error=str(exc)
                    )
        finally:
            await conn.execute(
                text("select pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": _LOCK_KEY}
            )
    return changed


async def _check(session: AsyncSession, ctx: RequestContext, row: Any, now: datetime) -> int:
    # The same lock as the Incident Manager, so this never races an alert for the tenant.
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"incidents:{ctx.tenant_id}"},
    )
    fingerprint = f"watchdog:{row.integration_id}"
    open_id = await session.scalar(_OPEN, {"fp": fingerprint})
    deadline = row.last_heartbeat_at + timedelta(seconds=MISSED * row.interval_s)
    silent = row.status == "active" and now > deadline
    if silent and open_id is None:
        await open_incident(
            session,
            ctx,
            ref={"name": f"platform:{row.name}"},
            source="watchdog",
            title=f"Visibility lost: no Watchdog heartbeat from {row.name}",
            severity="high",
            fingerprint=fingerprint,
            at=now,
            reason="watchdog heartbeat missing",
            resource={"integration_id": row.integration_id},
        )
        return 1
    if not silent and open_id is not None:
        reason = "heartbeat_restored" if row.status == "active" else "integration_disabled"
        await transition(session, ctx, open_id, "RESOLVED", reason, now)
        return 1
    return 0


async def run(engine: AsyncEngine, system_engine: AsyncEngine, clock: Clock) -> None:
    """Sweep until cancelled; a database outage only skips a round."""
    while True:
        try:
            await sweep(engine, system_engine, clock)
        except (SQLAlchemyError, OSError) as exc:
            log.warning("watchdog.unavailable", error=type(exc).__name__)
        await asyncio.sleep(INTERVAL_S)
