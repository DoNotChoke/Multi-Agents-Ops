"""Service mappings: tenant rules that file alerts under a catalog service by their labels."""

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response
from opsteam_core.context import RequestContext
from opsteam_core.errors import OpsteamError
from opsteam_core.ids import new_id
from opsteam_db import audit, tenant_session
from opsteam_incidents.normalize import MAPPINGS_SQL
from opsteam_incidents.state import record_activity
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from opsteam_api.deps import require_scope
from opsteam_api.routers.incidents import IncidentNotFound, ServiceRef
from opsteam_api.routers.services import ServiceNotFound

router = APIRouter(prefix="/api/v1", tags=["catalog"])

Reader = Annotated[RequestContext, Depends(require_scope("catalog:read"))]
Writer = Annotated[RequestContext, Depends(require_scope("catalog:write"))]
MappingId = Annotated[str, Path(pattern=r"^rule_[0-9A-HJKMNP-TV-Z]{26}$")]
Label = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Value = Annotated[str, StringConstraints(max_length=256)]

_SELECT = """
    select m.mapping_id, m.matchers, m.priority, m.created_at,
           s.service_id, s.name, s.environment
    from service_mappings m join services s on s.service_id = m.service_id
"""


class MappingExists(OpsteamError):
    code = "service_mapping.exists"


class MappingNotFound(OpsteamError):
    code = "service_mapping.not_found"


class NewMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Exact matches only: every pair must equal the alert's label.
    matchers: Annotated[dict[Label, Value], Field(min_length=1, max_length=10)]
    service_id: Annotated[str, Field(pattern=r"^svc_[0-9A-HJKMNP-TV-Z]{26}$")]
    priority: Annotated[int, Field(ge=-1000, le=1000)] = 0
    # The unmapped incident this rule was made from; it is filed under the service at once.
    incident_id: Annotated[str, Field(pattern=r"^inc_[0-9A-HJKMNP-TV-Z]{26}$")] | None = None


class Mapping(BaseModel):
    mapping_id: str
    matchers: dict[str, str]
    service: ServiceRef
    priority: int
    created_at: datetime


class MappingList(BaseModel):
    items: list[Mapping]


def _out(row: Any) -> Mapping:
    return Mapping(
        mapping_id=row.mapping_id,
        matchers=row.matchers,
        service=ServiceRef(service_id=row.service_id, name=row.name, environment=row.environment),
        priority=row.priority,
        created_at=row.created_at,
    )


@router.get("/service-mappings")
async def list_mappings(request: Request, ctx: Reader) -> MappingList:
    """The order is the one the Incident Manager tries them in."""
    async with tenant_session(request.app.state.engine, ctx) as session:
        rows = await session.execute(text(MAPPINGS_SQL))
        return MappingList(items=[_out(r) for r in rows])


@router.post("/service-mappings", status_code=201)
async def create_mapping(request: Request, ctx: Writer, body: NewMapping) -> Mapping:
    mapping_id = new_id("rule")
    async with tenant_session(request.app.state.engine, ctx) as session:
        if not await session.scalar(
            text("select 1 from services where service_id = :s"), {"s": body.service_id}
        ):
            raise ServiceNotFound("service not found")
        inserted = await session.scalar(
            text("""
                insert into service_mappings (mapping_id, tenant_id, matchers, service_id,
                                              priority, created_by)
                values (:id, :t, cast(:matchers as jsonb), :s, :p, :by)
                on conflict (tenant_id, matchers) do nothing
                returning mapping_id
            """),
            {
                "id": mapping_id,
                "t": ctx.tenant_id,
                "matchers": json.dumps(body.matchers),
                "s": body.service_id,
                "p": body.priority,
                "by": f"{ctx.principal.type}:{ctx.principal.id}",
            },
        )
        if inserted is None:
            raise MappingExists("a mapping with these matchers exists")
        resource = {
            "type": "service_mapping",
            "mapping_id": mapping_id,
            "service_id": body.service_id,
        }
        await audit.record(session, ctx, "service_mapping.created", resource, "allowed")
        if body.incident_id is not None:
            await _file_incident(session, ctx, body.incident_id, body.service_id)
        row = (
            await session.execute(text(_SELECT + " where m.mapping_id = :id"), {"id": mapping_id})
        ).one()
    return _out(row)


async def _file_incident(
    session: AsyncSession, ctx: RequestContext, incident_id: str, service_id: str
) -> None:
    filed = await session.scalar(
        text("""
            update incidents set service_id = :s, unmapped_service = null
            where incident_id = :i returning incident_id
        """),
        {"s": service_id, "i": incident_id},
    )
    if filed is None:  # rolls back the new mapping too
        raise IncidentNotFound("incident not found")
    await record_activity(session, ctx, incident_id, "incident.updated", service_id)
    resource = {"type": "incident", "incident_id": incident_id, "service_id": service_id}
    await audit.record(session, ctx, "incident.service_mapped", resource, "allowed")


@router.delete("/service-mappings/{mapping_id}", status_code=204)
async def delete_mapping(request: Request, ctx: Writer, mapping_id: MappingId) -> Response:
    async with tenant_session(request.app.state.engine, ctx) as session:
        deleted = await session.scalar(
            text("delete from service_mappings where mapping_id = :id returning mapping_id"),
            {"id": mapping_id},
        )
        if deleted is None:
            raise MappingNotFound("mapping not found")
        resource = {"type": "service_mapping", "mapping_id": mapping_id}
        await audit.record(session, ctx, "service_mapping.deleted", resource, "allowed")
    return Response(status_code=204)
