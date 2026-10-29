"""First notification: triage a new incident, then tell every active webhook channel about it."""

from typing import Any

import structlog
from opsteam_core.clock import Clock
from opsteam_core.context import RequestContext
from opsteam_core.errors import NetguardBlocked
from opsteam_core.events import Handler
from opsteam_db import tenant_session
from opsteam_db.secrets import SecretNotFound, SecretStore, SecretUnreadable
from opsteam_incidents.state import transition
from opsteam_notify.throttle import Throttle
from opsteam_notify.webhook import Channel, DeliveryFailed, Message, Notifier
from opsteam_obs.metrics import TIME_TO_DETECT, TIME_TO_NOTIFY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = structlog.get_logger()

_INCIDENT = text("""
    select i.incident_id, i.title, i.severity, i.status, i.first_notified_at, i.detected_at,
           coalesce(s.name, i.unmapped_service->>'name') as service,
           coalesce(s.environment, i.unmapped_service->>'environment') as environment,
           (select min(e.starts_at) from incident_events ie join events e using (event_id)
            where ie.incident_id = i.incident_id) as started_at
    from incidents i left join services s on s.service_id = i.service_id
    where i.incident_id = :id
""")
_CHANNELS = text("""
    select integration_id, endpoint, credentials_ref from integrations
    where type = 'webhook' and status = 'active' and endpoint is not null
    order by integration_id
""")


def opened_consumer(
    engine: AsyncEngine,
    secrets: SecretStore,
    notifier: Notifier,
    throttle: Throttle,
    clock: Clock,
    app_url: str,
) -> Handler:
    """Return the `incident.opened` handler; it raises after any failed channel so the outbox
    retries, and channels that already got the message are skipped on the retry."""

    async def consume(ctx: RequestContext, payload: dict[str, Any]) -> None:
        incident_id = payload.get("incident_id")
        if not isinstance(incident_id, str):
            log.warning("notify.bad_payload", tenant_id=ctx.tenant_id)
            return
        async with tenant_session(engine, ctx) as session:
            incident = (await session.execute(_INCIDENT, {"id": incident_id})).one_or_none()
            if incident is None or incident.status in ("RESOLVED", "CLOSED"):
                return
            if incident.status == "NEW":
                reason = "triaged: first notification"
                await transition(session, ctx, incident_id, "TRIAGED", reason, clock.now())
                if incident.started_at is not None:  # visibility_lost has no alert to time
                    detect = (incident.detected_at - incident.started_at).total_seconds()
                    TIME_TO_DETECT.observe(max(detect, 0.0))
            channels = (await session.execute(_CHANNELS)).all()
        message = Message(
            kind="incident.opened",
            incident_id=incident_id,
            title=incident.title,
            severity=incident.severity,
            service=incident.service,
            environment=incident.environment,
            started_at=incident.started_at,
            url=f"{app_url}/incidents/{incident_id}",
        )
        digest, delivered, failed = message.digest(), 0, 0
        for row in channels:
            channel_id = row.integration_id
            if not await throttle.allow(ctx.tenant_id, channel_id, incident_id, digest):
                continue
            try:
                secret = (
                    await secrets.get(ctx, row.credentials_ref) if row.credentials_ref else None
                )
                await notifier.send(Channel(channel_id, row.endpoint, secret), message)
            except (DeliveryFailed, NetguardBlocked, SecretNotFound, SecretUnreadable) as exc:
                log.warning("notify.failed", channel_id=channel_id, error=str(exc))
                failed += 1
                continue
            await throttle.sent(ctx.tenant_id, channel_id, incident_id, digest)
            delivered += 1
        if delivered and incident.first_notified_at is None:
            now = clock.now()
            async with tenant_session(engine, ctx) as session:
                first = await session.scalar(
                    text(
                        "update incidents set first_notified_at = :at"
                        " where incident_id = :id and first_notified_at is null"
                        " returning incident_id"
                    ),
                    {"at": now, "id": incident_id},
                )
            if first is not None:
                TIME_TO_NOTIFY.observe(max((now - incident.detected_at).total_seconds(), 0.0))
        if failed:
            raise DeliveryFailed(f"{failed} channel(s) failed")

    return consume
