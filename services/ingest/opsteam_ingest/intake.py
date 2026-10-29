"""Alertmanager alerts to stored events and outbox rows, for the webhook and reconciliation."""

import json
from datetime import UTC, datetime
from typing import Any

import structlog
from opsteam_core.context import RequestContext
from opsteam_db import tenant_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from opsteam_ingest.alertmanager import MAX_ALERTS, Alert, Payload, to_events

WATCHDOG = "Watchdog"
__all__ = ["MAX_ALERTS", "WATCHDOG", "store"]

log = structlog.get_logger()

_INSERT_EVENT = text("""
    insert into events (event_id, tenant_id, integration_id, source_type, fingerprint, status,
                        severity_hint, title, labels, annotations, starts_at, ends_at,
                        received_at)
    values (:event_id, :tenant_id, :integration_id, :source_type, :fingerprint, :status,
            :severity_hint, :title, cast(:labels as jsonb), cast(:annotations as jsonb),
            :starts_at, :ends_at, :received_at)
    on conflict (tenant_id, integration_id, fingerprint, status, starts_at) do nothing
    returning event_id
""")
_INSERT_OUTBOX = text("""
    insert into outbox (tenant_id, topic, payload)
    values (:tenant_id, 'event.received', cast(:payload as jsonb))
""")


async def store(
    engine: AsyncEngine,
    ctx: RequestContext,
    integration_id: str,
    config: dict[str, Any],
    alerts: list[Alert],
    *,
    heartbeat: bool = False,
) -> tuple[int, int]:
    """Insert new events with their outbox rows in one transaction; return accepted, duplicates.

    Watchdog alerts never become events; `heartbeat` stamps the integration in the same write.
    """
    events, dropped = to_events(
        Payload(alerts=[a for a in alerts if a.labels.get("alertname") != WATCHDOG]),
        ctx.tenant_id,
        integration_id,
        collapse=bool(config.get("collapse_replica_labels", True)),
        received_at=datetime.now(UTC),
    )
    if dropped:
        log.warning("intake.alerts_dropped", integration_id=integration_id, dropped=dropped)
    accepted = 0
    async with tenant_session(engine, ctx) as session:
        if heartbeat:
            await session.execute(
                text("update integrations set last_heartbeat_at = now() where integration_id = :i"),
                {"i": integration_id},
            )
        for event in events:
            params = event | {
                "labels": json.dumps(event["labels"]),
                "annotations": json.dumps(event["annotations"]),
            }
            if (await session.execute(_INSERT_EVENT, params)).one_or_none() is None:
                continue
            accepted += 1
            outbox = {
                "tenant_id": ctx.tenant_id,
                "payload": json.dumps({"event_id": event["event_id"]}),
            }
            await session.execute(_INSERT_OUTBOX, outbox)
    return accepted, len(events) - accepted
