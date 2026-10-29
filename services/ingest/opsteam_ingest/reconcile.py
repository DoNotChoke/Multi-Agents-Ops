"""Reconciliation: read each Alertmanager's active alerts back and store any the webhook lost."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from opsteam_core.context import Principal, RequestContext
from opsteam_core.errors import NetguardBlocked
from opsteam_db import tenant_session
from opsteam_obs.metrics import RECONCILIATION_GAP
from opsteam_tools.connectors.alertmanager import AlertmanagerConnector
from opsteam_tools.connectors.http import ConnectorError
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from opsteam_ingest import intake
from opsteam_ingest.alertmanager import Alert

PRINCIPAL = Principal(type="system", id="reconcile")
INTERVAL_S = 120.0
_LOCK_KEY = "opsteam:reconcile"

Fetch = Callable[[str], Awaitable[list[dict[str, Any]]]]

log = structlog.get_logger()

_FAILED = text(
    "update integrations set reconcile_failures = reconcile_failures + 1 where integration_id = :i"
)
_READ = text("update integrations set reconcile_failures = 0 where integration_id = :i")


def _as_firing(raw: dict[str, Any]) -> Alert | None:
    """An API v2 alert as the webhook would have sent it, marked as reconciled."""
    try:
        alert = Alert.model_validate(raw | {"status": "firing", "endsAt": None})
    except ValidationError:
        return None
    # An annotation, not a label: labels make the fingerprint the webhook copy must share.
    return alert.model_copy(update={"annotations": alert.annotations | {"reconciled": "true"}})


async def reconcile_once(engine: AsyncEngine, system_engine: AsyncEngine, fetch: Fetch) -> int:
    """Read every active Alertmanager with an endpoint once; return how many alerts were missing."""
    filled = 0
    async with system_engine.connect() as conn:
        locked = await conn.scalar(
            text("select pg_try_advisory_lock(hashtextextended(:k, 0))"), {"k": _LOCK_KEY}
        )
        if not locked:
            return 0
        try:
            targets = (await conn.execute(text("select * from app_reconcile_targets()"))).all()
            for row in targets:
                ctx = RequestContext(
                    tenant_id=row.tenant_id, principal=PRINCIPAL, roles=(), scopes=frozenset()
                )
                try:
                    filled += await _reconcile(engine, ctx, row, fetch)
                except (SQLAlchemyError, OSError) as exc:
                    log.warning(
                        "reconcile.store_failed", integration_id=row.integration_id, error=str(exc)
                    )
        finally:
            await conn.execute(
                text("select pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": _LOCK_KEY}
            )
    return filled


async def _reconcile(engine: AsyncEngine, ctx: RequestContext, row: Any, fetch: Fetch) -> int:
    try:
        raw = await fetch(row.endpoint)
    except (ConnectorError, NetguardBlocked) as exc:
        log.warning("reconcile.read_failed", integration_id=row.integration_id, error=str(exc))
        async with tenant_session(engine, ctx) as session:
            await session.execute(_FAILED, {"i": row.integration_id})
        return 0
    alerts = [a for a in map(_as_firing, raw) if a is not None]
    if len(alerts) < len(raw):
        log.warning("reconcile.alerts_skipped", integration_id=row.integration_id)
    accepted = 0
    # intake.store caps each call, so a long list goes in slices.
    for start in range(0, len(alerts), intake.MAX_ALERTS):
        added, _ = await intake.store(
            engine, ctx, row.integration_id, row.config, alerts[start : start + intake.MAX_ALERTS]
        )
        accepted += added
    async with tenant_session(engine, ctx) as session:
        await session.execute(_READ, {"i": row.integration_id})
    if accepted:
        RECONCILIATION_GAP.inc(accepted)
        log.warning("reconcile.gap_filled", integration_id=row.integration_id, alerts=accepted)
    return accepted


async def run(engine: AsyncEngine, system_engine: AsyncEngine, dev_allow: frozenset[str]) -> None:
    """Reconcile until cancelled; a database outage only skips a round."""

    async def fetch(endpoint: str) -> list[dict[str, Any]]:
        return await AlertmanagerConnector(endpoint, dev_allow=dev_allow).alerts()

    while True:
        try:
            await reconcile_once(engine, system_engine, fetch)
        except (SQLAlchemyError, OSError) as exc:
            log.warning("reconcile.unavailable", error=type(exc).__name__)
        await asyncio.sleep(INTERVAL_S)
