"""Incident Manager: turns each received event into a new or existing incident, exactly once."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from opsteam_core.context import RequestContext
from opsteam_core.events import Handler
from opsteam_core.ids import new_id
from opsteam_db import audit, tenant_session
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from opsteam_incidents.normalize import resolve_service
from opsteam_incidents.severity import severity

CORRELATION_WINDOW = timedelta(minutes=15)
PROPAGATION_WINDOW = timedelta(minutes=5)
PRODUCTION = frozenset({"production", "prod"})
_SAME_FINGERPRINT = text("""
    select incident_id from incidents
    where root_fingerprint = :fp and status not in ('RESOLVED', 'CLOSED')
    order by opened_at desc limit 1
""")
_SAME_SERVICE = text("""
    select incident_id from incidents
    where service_id = :svc and status not in ('RESOLVED', 'CLOSED') and opened_at >= :since
    order by opened_at desc limit 1
""")
# Services within two hops in either direction, with an open incident whose alerts began
# close to this one: likely one failure spreading along the call graph.
_NEIGHBOUR = text("""
    with recursive near (service_id, depth) as (
      select cast(:svc as text), 0
      union
      select case when d.service_id = n.service_id
                  then d.depends_on_service_id else d.service_id end, n.depth + 1
      from near n
      join service_dependencies d on n.service_id in (d.service_id, d.depends_on_service_id)
      where n.depth < 2
    )
    select i.incident_id, i.service_id from incidents i
    where i.service_id in (select service_id from near where service_id <> :svc)
      and i.status not in ('RESOLVED', 'CLOSED') and i.opened_at >= :since
      and exists (
        select 1 from incident_events ie join events e on e.event_id = ie.event_id
        where ie.incident_id = i.incident_id
          and abs(extract(epoch from e.starts_at - :starts)) <= :propagation
      )
    order by i.opened_at desc limit 1
""")
# Whether :svc is something :down calls, directly or through one more service.
_IS_UPSTREAM = text("""
    with recursive up (service_id, depth) as (
      select cast(:down as text), 0
      union
      select d.depends_on_service_id, u.depth + 1
      from up u join service_dependencies d on d.service_id = u.service_id
      where u.depth < 2
    )
    select exists (select 1 from up where service_id = :svc and depth > 0)
""")
_FIRING_OF = text("""
    select ie.incident_id from incident_events ie
    join events e on e.event_id = ie.event_id
    join incidents i on i.incident_id = ie.incident_id
    where e.fingerprint = :fp and e.status = 'firing'
    order by i.opened_at desc limit 1
""")


def event_consumer(engine: AsyncEngine) -> Handler:
    """Return the `event.received` handler; redelivered events are skipped."""

    async def consume(ctx: RequestContext, payload: dict[str, Any]) -> None:
        async with tenant_session(engine, ctx) as session:
            await _process(session, ctx, str(payload["event_id"]))

    return consume


async def _process(session: AsyncSession, ctx: RequestContext, event_id: str) -> None:
    # One tenant's events are correlated one at a time, so two can never open twin incidents.
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"incidents:{ctx.tenant_id}"},
    )
    event = (
        await session.execute(
            text("""
                select event_id, fingerprint, status, title, labels, starts_at, received_at,
                       processed_at
                from events where event_id = :id for update
            """),
            {"id": event_id},
        )
    ).one_or_none()
    if event is None or event.processed_at is not None:
        return
    services = await session.execute(text("select service_id, name, environment from services"))
    ref = resolve_service(event.labels, {(n, e): i for i, n, e in services})
    await session.execute(
        text(
            "update events set service_ref = cast(:ref as jsonb), processed_at = :now"
            " where event_id = :id"
        ),
        {"ref": json.dumps(ref), "now": datetime.now(UTC), "id": event_id},
    )
    if event.status == "firing":
        await _firing(session, ctx, event, ref)
    else:
        incident_id = await session.scalar(_FIRING_OF, {"fp": event.fingerprint})
        if incident_id is not None:
            await _attach(session, ctx, incident_id, event, "resolution")


async def _firing(
    session: AsyncSession, ctx: RequestContext, event: Row[Any], ref: dict[str, str]
) -> None:
    service_id = ref.get("service_id")
    incident_id, rule = await _correlate(session, event, service_id)
    if incident_id is None:
        incident_id, rule = await _open(session, ctx, event, ref), "new"
    await _attach(session, ctx, incident_id, event, rule)
    await _rescore(session, incident_id)


async def _correlate(
    session: AsyncSession, event: Row[Any], service_id: str | None
) -> tuple[str | None, str]:
    """Return the open incident this event belongs to and the rule that matched, if any."""
    if incident_id := await session.scalar(_SAME_FINGERPRINT, {"fp": event.fingerprint}):
        return incident_id, "fingerprint"
    if service_id is None:
        return None, ""
    since = event.received_at - CORRELATION_WINDOW
    if incident_id := await session.scalar(_SAME_SERVICE, {"svc": service_id, "since": since}):
        return incident_id, "service"
    neighbour = (
        await session.execute(
            _NEIGHBOUR,
            {
                "svc": service_id,
                "since": since,
                "starts": event.starts_at,
                "propagation": PROPAGATION_WINDOW.total_seconds(),
            },
        )
    ).one_or_none()
    if neighbour is None:
        return None, ""
    # The root is the service further upstream: what the others are waiting on.
    if await session.scalar(_IS_UPSTREAM, {"down": neighbour.service_id, "svc": service_id}):
        await session.execute(
            text("update incidents set service_id = :svc where incident_id = :id"),
            {"svc": service_id, "id": neighbour.incident_id},
        )
    return neighbour.incident_id, "dependency"


async def _open(
    session: AsyncSession, ctx: RequestContext, event: Row[Any], ref: dict[str, str]
) -> str:
    incident_id = new_id("inc")
    await session.execute(
        text("""
            insert into incidents (incident_id, tenant_id, service_id, unmapped_service, source,
                                   title, status, severity, root_fingerprint, opened_at,
                                   detected_at, created_by)
            values (:id, :t, :svc, cast(:unmapped as jsonb), 'alert', :title, 'NEW', 'info',
                    :fp, :at, :at, :by)
        """),
        {
            "id": incident_id,
            "t": ctx.tenant_id,
            "svc": ref.get("service_id"),
            "unmapped": json.dumps(ref) if ref and "service_id" not in ref else None,
            "title": event.title,
            "fp": event.fingerprint,
            "at": event.received_at,
            "by": f"{ctx.principal.type}:{ctx.principal.id}",
        },
    )
    actor = json.dumps({"type": ctx.principal.type, "id": ctx.principal.id})
    params = {"t": ctx.tenant_id, "id": incident_id, "at": event.received_at, "actor": actor}
    await session.execute(
        text("""
            insert into incident_transitions
              (tenant_id, incident_id, from_status, to_status, reason, actor, at)
            values (:t, :id, null, 'NEW', 'opened from an alert', cast(:actor as jsonb), :at)
        """),
        params,
    )
    await session.execute(
        text("""
            insert into incident_activity (tenant_id, incident_id, type, ref_id, at)
            values (:t, :id, 'incident.opened', :id, :at)
        """),
        params,
    )
    await session.execute(
        text("""
            insert into outbox (tenant_id, topic, payload)
            values (:t, 'incident.opened', cast(:payload as jsonb))
        """),
        {"t": ctx.tenant_id, "payload": json.dumps({"incident_id": incident_id})},
    )
    resource = {"type": "incident", "incident_id": incident_id, "event_id": event.event_id}
    await audit.record(session, ctx, "incident.opened", resource, "allowed")
    return incident_id


async def _attach(
    session: AsyncSession, ctx: RequestContext, incident_id: str, event: Row[Any], rule: str
) -> None:
    await session.execute(
        text("""
            insert into incident_events (incident_id, event_id, tenant_id)
            values (:i, :e, :t) on conflict do nothing
        """),
        {"i": incident_id, "e": event.event_id, "t": ctx.tenant_id},
    )
    await session.execute(
        text("""
            insert into incident_activity (tenant_id, incident_id, type, ref_id)
            values (:t, :i, 'incident.event_attached', :e)
        """),
        {"t": ctx.tenant_id, "i": incident_id, "e": event.event_id},
    )
    if rule != "new":  # opening already explained itself
        resource = {"type": "incident", "incident_id": incident_id, "event_id": event.event_id}
        await audit.record(
            session, ctx, "incident.event_attached", resource | {"rule": rule}, "allowed"
        )


async def _rescore(session: AsyncSession, incident_id: str) -> None:
    firing = (
        await session.execute(
            text("""
                select e.severity_hint, e.labels->>'slo_burn' as burn,
                       coalesce(e.service_ref->>'service_id', e.service_ref->>'name') as service
                from incident_events ie join events e on e.event_id = ie.event_id
                where ie.incident_id = :i and e.status = 'firing'
            """),
            {"i": incident_id},
        )
    ).all()
    context = (
        await session.execute(
            text("""
                select s.environment, s.criticality from incidents i
                left join services s on s.service_id = i.service_id
                where i.incident_id = :i
            """),
            {"i": incident_id},
        )
    ).one()
    level = severity(
        (row.severity_hint for row in firing),
        production_high=context.environment in PRODUCTION
        and context.criticality in ("high", "critical"),
        services_affected=len({row.service for row in firing if row.service}),
        slo_fast_burn=any(row.burn == "fast" for row in firing),
    )
    await session.execute(
        text("update incidents set severity = :s where incident_id = :i"),
        {"s": level, "i": incident_id},
    )
