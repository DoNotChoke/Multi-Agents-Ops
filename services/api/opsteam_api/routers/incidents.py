"""Read incidents: a filtered list, and one incident with its timeline and alerts."""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from opsteam_core.context import RequestContext
from opsteam_core.errors import OpsteamError
from opsteam_db import tenant_session
from pydantic import BaseModel
from sqlalchemy import text

from opsteam_api.deps import require_scope

router = APIRouter(prefix="/api/v1", tags=["incidents"])

Reader = Annotated[RequestContext, Depends(require_scope("incident:read"))]
IncidentId = Annotated[str, Path(pattern=r"^inc_[0-9A-HJKMNP-TV-Z]{26}$")]
Status = Literal[
    "NEW", "TRIAGED", "INVESTIGATING", "DIAGNOSED", "INCONCLUSIVE", "REMEDIATION_PROPOSED",
    "AWAITING_APPROVAL", "EXECUTING", "VERIFYING", "MANUAL_ACTION_REQUIRED", "RESOLVED", "CLOSED",
]  # fmt: skip
Severity = Literal["info", "low", "medium", "high", "critical"]
MAX_EVENTS = 200

_SUMMARY = """
    select i.incident_id, i.title, i.status, i.severity, i.unmapped_service, i.opened_at,
           i.resolved_at, i.detected_at, i.first_notified_at,
           s.service_id, s.name as service_name, s.environment,
           (select count(*) from incident_events ie where ie.incident_id = i.incident_id)
             as event_count
    from incidents i left join services s on s.service_id = i.service_id
"""


class IncidentNotFound(OpsteamError):
    code = "incident.not_found"


class ServiceRef(BaseModel):
    service_id: str
    name: str
    environment: str


class IncidentSummary(BaseModel):
    incident_id: str
    title: str
    status: str
    severity: str
    # The catalog service, or the raw names from the alert labels when none matched.
    service: ServiceRef | None
    unmapped_service: dict[str, str] | None
    opened_at: datetime
    resolved_at: datetime | None
    event_count: int


class IncidentPage(BaseModel):
    items: list[IncidentSummary]
    next_cursor: str | None


class Transition(BaseModel):
    from_status: str | None
    to_status: str
    reason: str
    actor: dict[str, Any]
    at: datetime


class IncidentEvent(BaseModel):
    """An alert attached to the incident; labels and annotations come from the customer."""

    event_id: str
    status: str
    title: str
    severity_hint: str | None
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: datetime
    ends_at: datetime | None
    received_at: datetime


class Incident(IncidentSummary):
    detected_at: datetime
    first_notified_at: datetime | None
    started_at: datetime | None
    time_to_detect_seconds: float | None
    timeline: list[Transition]
    events: list[IncidentEvent]
    events_truncated: bool


def _summary(row: Any) -> dict[str, Any]:
    fields = dict(row._mapping)
    service_id, name, environment = (
        fields.pop("service_id"),
        fields.pop("service_name"),
        fields.pop("environment"),
    )
    fields["service"] = (
        ServiceRef(service_id=service_id, name=name, environment=environment)
        if service_id
        else None
    )
    return fields


@router.get("/incidents")
async def list_incidents(
    request: Request,
    ctx: Reader,
    status: Annotated[list[Status] | None, Query()] = None,
    severity: Annotated[list[Severity] | None, Query()] = None,
    service_id: Annotated[str | None, Query(pattern=r"^svc_[0-9A-HJKMNP-TV-Z]{26}$")] = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(pattern=r"^inc_[0-9A-HJKMNP-TV-Z]{26}$")] = None,
) -> IncidentPage:
    """Newest first; ids are ULIDs, so the last id of a page is the cursor for the next."""
    async with tenant_session(request.app.state.engine, ctx) as session:
        rows = (
            await session.execute(
                text(
                    _SUMMARY
                    + """
                    where (cast(:cursor as text) is null or i.incident_id < :cursor)
                      and (cast(:statuses as text[]) is null or i.status = any(:statuses))
                      and (cast(:severities as text[]) is null or i.severity = any(:severities))
                      and (cast(:service_id as text) is null or i.service_id = :service_id)
                      and (cast(:since as timestamptz) is null or i.opened_at >= :since)
                      and (cast(:until as timestamptz) is null or i.opened_at < :until)
                    order by i.incident_id desc
                    limit :limit
                    """
                ),
                {
                    "cursor": cursor,
                    "statuses": status,
                    "severities": severity,
                    "service_id": service_id,
                    "since": since,
                    "until": until,
                    "limit": limit + 1,
                },
            )
        ).all()
    page = rows[:limit]
    return IncidentPage(
        items=[IncidentSummary.model_validate(_summary(r)) for r in page],
        next_cursor=page[-1].incident_id if len(rows) > limit else None,
    )


@router.get("/incidents/{incident_id}")
async def get_incident(request: Request, ctx: Reader, incident_id: IncidentId) -> Incident:
    async with tenant_session(request.app.state.engine, ctx) as session:
        row = (
            await session.execute(
                text(_SUMMARY + " where i.incident_id = :id"),
                {"id": incident_id},
            )
        ).one_or_none()
        if row is None:
            raise IncidentNotFound("incident not found")
        timeline = (
            await session.execute(
                text("""
                    select from_status, to_status, reason, actor, at from incident_transitions
                    where incident_id = :id order by at, transition_id
                """),
                {"id": incident_id},
            )
        ).all()
        events = (
            await session.execute(
                text("""
                    select e.event_id, e.status, e.title, e.severity_hint, e.labels,
                           e.annotations, e.starts_at, e.ends_at, e.received_at
                    from incident_events ie join events e on e.event_id = ie.event_id
                    where ie.incident_id = :id
                    order by e.received_at, e.event_id
                    limit :limit
                """),
                {"id": incident_id, "limit": MAX_EVENTS + 1},
            )
        ).all()
        started_at = await session.scalar(
            text("""
                select min(e.starts_at) from incident_events ie
                join events e on e.event_id = ie.event_id where ie.incident_id = :id
            """),
            {"id": incident_id},
        )
    fields = _summary(row)
    return Incident(
        **fields,
        started_at=started_at,
        time_to_detect_seconds=(
            (row.detected_at - started_at).total_seconds() if started_at else None
        ),
        timeline=[Transition.model_validate(dict(t._mapping)) for t in timeline],
        events=[IncidentEvent.model_validate(dict(e._mapping)) for e in events[:MAX_EVENTS]],
        events_truncated=len(events) > MAX_EVENTS,
    )
