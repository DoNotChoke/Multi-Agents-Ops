"""Service catalog: the tenant's services, named after OpenTelemetry, and their dependencies."""

import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Request, Response
from opsteam_core.context import RequestContext
from opsteam_core.errors import NetguardBlocked, OpsteamError
from opsteam_core.ids import new_id
from opsteam_db import audit, tenant_session
from opsteam_tools.connectors.prometheus import ConnectorError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from opsteam_api.deps import require_scope
from opsteam_api.routers.integrations import IntegrationUnavailable, prometheus_for

router = APIRouter(prefix="/api/v1", tags=["catalog"])

# Same alphabet as query-template parameters, so a catalog name is always safe to put in a query.
NAME = re.compile(r"[A-Za-z0-9_.\-/:]{1,128}")
Name = Annotated[str, Field(pattern=f"^{NAME.pattern}$")]
ServiceId = Annotated[str, Path(pattern=r"^svc_[0-9A-HJKMNP-TV-Z]{26}$")]
Criticality = Literal["low", "medium", "high", "critical"]
Reader = Annotated[RequestContext, Depends(require_scope("catalog:read"))]
Writer = Annotated[RequestContext, Depends(require_scope("catalog:write"))]

_COLUMNS = "service_id, name, environment, owner, repository, criticality"


class ServiceExists(OpsteamError):
    code = "service.exists"


class ServiceNotFound(OpsteamError):
    code = "service.not_found"


class SelfDependency(OpsteamError):
    code = "service.self_dependency"


class NewService(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    environment: Name
    owner: str | None = Field(default=None, max_length=200)
    repository: str | None = Field(default=None, max_length=500)
    criticality: Criticality | None = None


class ServiceChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    environment: Name | None = None
    owner: str | None = Field(default=None, max_length=200)
    repository: str | None = Field(default=None, max_length=500)
    criticality: Criticality | None = None


class Service(BaseModel):
    service_id: str
    name: str
    environment: str
    owner: str | None
    repository: str | None
    criticality: str | None


class ServiceList(BaseModel):
    items: list[Service]


class NewDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    depends_on_service_id: Annotated[str, Field(pattern=r"^svc_[0-9A-HJKMNP-TV-Z]{26}$")]


class Edge(BaseModel):
    from_: str = Field(serialization_alias="from")
    to: str


class Topology(BaseModel):
    nodes: list[Service]
    edges: list[Edge]


@router.get("/services")
async def list_services(request: Request, ctx: Reader) -> ServiceList:
    async with tenant_session(request.app.state.engine, ctx) as session:
        rows = await session.execute(
            text(f"select {_COLUMNS} from services order by name, environment")  # noqa: S608
        )
        return ServiceList(items=[Service.model_validate(dict(r)) for r in rows.mappings()])


@router.post("/services", status_code=201)
async def create_service(request: Request, ctx: Writer, body: NewService) -> Service:
    row = {"service_id": new_id("svc"), **body.model_dump()}
    async with tenant_session(request.app.state.engine, ctx) as session:
        created = await session.scalar(
            text(f"""
                insert into services (tenant_id, {_COLUMNS})
                values (:tenant_id, :service_id, :name, :environment, :owner, :repository,
                        :criticality)
                on conflict (tenant_id, name, environment) do nothing returning service_id
            """),  # noqa: S608
            row | {"tenant_id": ctx.tenant_id},
        )
        if created is None:
            raise ServiceExists("a service with this name and environment exists")
        await audit.record(session, ctx, "service.created", _ref(row), "allowed")
    return Service.model_validate(row)


@router.patch("/services/{service_id}")
async def update_service(
    request: Request, ctx: Writer, service_id: ServiceId, body: ServiceChange
) -> Service:
    changes = body.model_dump(exclude_unset=True)
    try:
        async with tenant_session(request.app.state.engine, ctx) as session:
            # Column names come from the model's fields, never from the request keys.
            sets = ", ".join(f"{k} = :{k}" for k in changes) or "service_id = service_id"
            sql = f"update services set {sets} where service_id = :id returning {_COLUMNS}"  # noqa: S608
            result = await session.execute(text(sql), changes | {"id": service_id})
            row = result.mappings().one_or_none()
            if row is None:
                raise ServiceNotFound("service not found")
            resource = _ref(dict(row)) | {"changed": sorted(changes)}
            await audit.record(session, ctx, "service.updated", resource, "allowed")
    except IntegrityError as exc:
        raise ServiceExists("a service with this name and environment exists") from exc
    return Service.model_validate(dict(row))


@router.post("/services/{service_id}/dependencies", status_code=201)
async def add_dependency(
    request: Request, ctx: Writer, service_id: ServiceId, body: NewDependency
) -> Edge:
    target = body.depends_on_service_id
    if target == service_id:
        raise SelfDependency("a service cannot depend on itself")
    async with tenant_session(request.app.state.engine, ctx) as session:
        found = await session.scalar(
            text("select count(*) from services where service_id in (:a, :b)"),
            {"a": service_id, "b": target},
        )
        if found != 2:
            raise ServiceNotFound("service not found")
        added = await session.scalar(
            text("""
                insert into service_dependencies (tenant_id, service_id, depends_on_service_id)
                values (:t, :a, :b) on conflict do nothing returning service_id
            """),
            {"t": ctx.tenant_id, "a": service_id, "b": target},
        )
        if added is not None:
            resource = {"type": "dependency", "from": service_id, "to": target}
            await audit.record(session, ctx, "dependency.added", resource, "allowed")
    return Edge(from_=service_id, to=target)


@router.delete("/services/{service_id}/dependencies/{depends_on_service_id}", status_code=204)
async def remove_dependency(
    request: Request, ctx: Writer, service_id: ServiceId, depends_on_service_id: ServiceId
) -> Response:
    async with tenant_session(request.app.state.engine, ctx) as session:
        removed = await session.scalar(
            text("""
                delete from service_dependencies
                where service_id = :a and depends_on_service_id = :b returning service_id
            """),
            {"a": service_id, "b": depends_on_service_id},
        )
        if removed is None:
            raise ServiceNotFound("dependency not found")
        resource = {"type": "dependency", "from": service_id, "to": depends_on_service_id}
        await audit.record(session, ctx, "dependency.removed", resource, "allowed")
    return Response(status_code=204)


@router.get("/services/topology")
async def topology(request: Request, ctx: Reader) -> Topology:
    async with tenant_session(request.app.state.engine, ctx) as session:
        nodes = await session.execute(
            text(f"select {_COLUMNS} from services order by name, environment")  # noqa: S608
        )
        edges = await session.execute(
            text("select service_id, depends_on_service_id from service_dependencies")
        )
        return Topology(
            nodes=[Service.model_validate(dict(r)) for r in nodes.mappings()],
            edges=[Edge(from_=a, to=b) for a, b in edges],
        )


def _ref(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("service_id", "name", "environment")
    return {"type": "service"} | {k: row[k] for k in keys}


# Cheap (only `up` series) and yields (service, namespace) pairs; written by code, never by input.
SUGGEST_QUERY = 'group by (service, namespace) (up{service!=""})'
MAX_SUGGESTIONS = 200


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    integration_id: Annotated[str, Field(pattern=r"^int_[0-9A-HJKMNP-TV-Z]{26}$")]


class Suggestion(BaseModel):
    name: str
    environment: str
    exists: bool


class Suggestions(BaseModel):
    suggestions: list[Suggestion]
    truncated: bool


@router.post("/services/import/prometheus")
async def suggest_services(request: Request, ctx: Writer, body: ImportRequest) -> Suggestions:
    """Propose catalog entries from Prometheus labels; nothing is saved until the user confirms."""
    prometheus = await prometheus_for(request, ctx, body.integration_id)
    try:
        data = await prometheus.query(SUGGEST_QUERY)
    except (ConnectorError, NetguardBlocked) as exc:
        raise IntegrationUnavailable(str(exc)) from exc
    # Labels come from the customer's system: keep only values a catalog name could hold.
    pairs = sorted(
        {
            (m.get("service", ""), m.get("namespace", ""))
            for m in (r.get("metric", {}) for r in data.get("result", []))
            if NAME.fullmatch(str(m.get("service", "")))
            and NAME.fullmatch(str(m.get("namespace", "")))
        }
    )
    async with tenant_session(request.app.state.engine, ctx) as session:
        rows = await session.execute(text("select name, environment from services"))
        existing = {(name, environment) for name, environment in rows}
    return Suggestions(
        suggestions=[
            Suggestion(name=n, environment=e, exists=(n, e) in existing)
            for n, e in pairs[:MAX_SUGGESTIONS]
        ],
        truncated=len(pairs) > MAX_SUGGESTIONS,
    )
