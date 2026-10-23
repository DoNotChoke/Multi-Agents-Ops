"""Integrations: where a tenant's telemetry lives; credentials go to the SecretStore only."""

import contextlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Request, Response
from opsteam_core.context import RequestContext
from opsteam_core.errors import NetguardBlocked, OpsteamError
from opsteam_core.ids import new_id
from opsteam_core.netguard import check_url
from opsteam_db import audit, tenant_session
from opsteam_db.secrets import SecretNotFound, SecretUnreadable
from opsteam_tools.connectors.prometheus import (
    PROMETHEUS_PORTS,
    ConnectorError,
    PrometheusConnector,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from opsteam_api.deps import require_scope

router = APIRouter(prefix="/api/v1", tags=["integrations"])

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.\-]{1,100}$")]
IntegrationId = Annotated[str, Path(pattern=r"^int_[0-9A-HJKMNP-TV-Z]{26}$")]
Reader = Annotated[RequestContext, Depends(require_scope("catalog:read"))]
Writer = Annotated[RequestContext, Depends(require_scope("integration:write"))]

_SELECT = """
    select integration_id, type, name, mode, status, endpoint, config, last_tested_at,
           created_at, credentials_ref is not null as has_credentials
    from integrations
"""


class IntegrationExists(OpsteamError):
    code = "integration.exists"


class IntegrationNotFound(OpsteamError):
    code = "integration.not_found"


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(max_length=2048)
    # Template name -> customer metric expression; set by admins, never by the LLM.
    metric_mapping: dict[str, Annotated[str, Field(max_length=1000)]] = Field(
        default_factory=dict, max_length=50
    )


class NewIntegration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["prometheus"]
    name: Name
    mode: Literal["direct"]
    config: Config
    credentials: Annotated[SecretStr, Field(max_length=4096)] | None = None


class IntegrationChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    config: Config | None = None
    status: Literal["active", "disabled"] | None = None


class Integration(BaseModel):
    integration_id: str
    type: str
    name: str
    mode: str
    status: str
    config: dict[str, Any]
    has_credentials: bool
    last_tested_at: datetime | None
    created_at: datetime


class IntegrationList(BaseModel):
    items: list[Integration]


class Check(BaseModel):
    name: str
    ok: bool
    detail: str


class TestReport(BaseModel):
    ok: bool
    checks: list[Check]


def _out(row: Any) -> Integration:
    fields = dict(row._mapping)
    fields["config"] = {"endpoint": fields.pop("endpoint"), **fields["config"]}
    return Integration.model_validate(fields)


def _ref(integration_id: str, name: str) -> dict[str, Any]:
    return {"type": "integration", "integration_id": integration_id, "name": name}


async def _vet(request: Request, endpoint: str) -> None:
    await check_url(endpoint, extra_ports=PROMETHEUS_PORTS, dev_allow=request.app.state.dev_allow)


@router.get("/integrations")
async def list_integrations(request: Request, ctx: Reader) -> IntegrationList:
    async with tenant_session(request.app.state.engine, ctx) as session:
        rows = await session.execute(text(_SELECT + " order by name"))
        return IntegrationList(items=[_out(r) for r in rows])


@router.get("/integrations/{integration_id}")
async def get_integration(
    request: Request, ctx: Reader, integration_id: IntegrationId
) -> Integration:
    async with tenant_session(request.app.state.engine, ctx) as session:
        row = (
            await session.execute(
                text(_SELECT + " where integration_id = :id"), {"id": integration_id}
            )
        ).one_or_none()
    if row is None:
        raise IntegrationNotFound("integration not found")
    return _out(row)


@router.post("/integrations", status_code=201)
async def create_integration(request: Request, ctx: Writer, body: NewIntegration) -> Integration:
    await _vet(request, body.config.endpoint)
    secrets = request.app.state.secrets
    ref = await secrets.put(ctx, body.credentials) if body.credentials else None
    integration_id = new_id("int")
    try:
        async with tenant_session(request.app.state.engine, ctx) as session:
            row = (
                await session.execute(
                    text("""
                        insert into integrations (integration_id, tenant_id, type, name, mode,
                                                  endpoint, config, credentials_ref)
                        values (:id, :tenant_id, :type, :name, :mode, :endpoint,
                                cast(:config as jsonb), :ref)
                        on conflict (tenant_id, name) do nothing
                        returning integration_id, type, name, mode, status, endpoint, config,
                                  last_tested_at, created_at,
                                  credentials_ref is not null as has_credentials
                    """),
                    {
                        "id": integration_id,
                        "tenant_id": ctx.tenant_id,
                        "type": body.type,
                        "name": body.name,
                        "mode": body.mode,
                        "endpoint": body.config.endpoint,
                        "config": json.dumps({"metric_mapping": body.config.metric_mapping}),
                        "ref": ref,
                    },
                )
            ).one_or_none()
            if row is None:
                raise IntegrationExists("an integration with this name exists")
            resource = _ref(integration_id, body.name) | {"has_credentials": ref is not None}
            await audit.record(session, ctx, "integration.created", resource, "allowed")
    except Exception:
        if ref is not None:  # don't leave an unreachable secret behind
            await secrets.delete(ctx, ref)
        raise
    return _out(row)


@router.patch("/integrations/{integration_id}")
async def update_integration(
    request: Request, ctx: Writer, integration_id: IntegrationId, body: IntegrationChange
) -> Integration:
    if body.config is not None:
        await _vet(request, body.config.endpoint)
    params: dict[str, Any] = {"id": integration_id, "name": body.name, "status": body.status}
    params["endpoint"] = body.config.endpoint if body.config else None
    params["config"] = (
        json.dumps({"metric_mapping": body.config.metric_mapping}) if body.config else None
    )
    try:
        async with tenant_session(request.app.state.engine, ctx) as session:
            row = (
                await session.execute(
                    text("""
                        update integrations set
                          name = coalesce(:name, name),
                          status = coalesce(:status, status),
                          endpoint = coalesce(:endpoint, endpoint),
                          config = coalesce(cast(:config as jsonb), config)
                        where integration_id = :id
                        returning integration_id, type, name, mode, status, endpoint, config,
                                  last_tested_at, created_at,
                                  credentials_ref is not null as has_credentials
                    """),
                    params,
                )
            ).one_or_none()
            if row is None:
                raise IntegrationNotFound("integration not found")
            changed = sorted(body.model_dump(exclude_unset=True))
            resource = _ref(integration_id, row.name) | {"changed": changed}
            await audit.record(session, ctx, "integration.updated", resource, "allowed")
    except IntegrityError as exc:
        raise IntegrationExists("an integration with this name exists") from exc
    return _out(row)


@router.delete("/integrations/{integration_id}", status_code=204)
async def delete_integration(
    request: Request, ctx: Writer, integration_id: IntegrationId
) -> Response:
    async with tenant_session(request.app.state.engine, ctx) as session:
        row = (
            await session.execute(
                text("""
                    delete from integrations where integration_id = :id
                    returning name, credentials_ref
                """),
                {"id": integration_id},
            )
        ).one_or_none()
        if row is None:
            raise IntegrationNotFound("integration not found")
        await audit.record(
            session, ctx, "integration.deleted", _ref(integration_id, row.name), "allowed"
        )
    if row.credentials_ref is not None:
        with contextlib.suppress(SecretNotFound):
            await request.app.state.secrets.delete(ctx, row.credentials_ref)
    return Response(status_code=204)


@router.post("/integrations/{integration_id}/test")
async def test_integration(
    request: Request, ctx: Writer, integration_id: IntegrationId
) -> TestReport:
    async with tenant_session(request.app.state.engine, ctx) as session:
        row = (
            await session.execute(
                text(
                    "select name, endpoint, credentials_ref from integrations"
                    " where integration_id = :id"
                ),
                {"id": integration_id},
            )
        ).one_or_none()
    if row is None:
        raise IntegrationNotFound("integration not found")
    checks = [await _endpoint_check(request, row.endpoint)]
    token: SecretStr | None = None
    if row.credentials_ref is None:
        checks.append(Check(name="credentials_readable", ok=True, detail="no credentials"))
    else:
        try:
            token = await request.app.state.secrets.get(ctx, row.credentials_ref)
            checks.append(Check(name="credentials_readable", ok=True, detail="credentials stored"))
        except (SecretNotFound, SecretUnreadable):
            checks.append(
                Check(name="credentials_readable", ok=False, detail="credentials unreadable")
            )
    if all(c.ok for c in checks):
        checks += await _live_checks(request, row.endpoint, token)
    report = TestReport(ok=all(c.ok for c in checks), checks=checks)
    async with tenant_session(request.app.state.engine, ctx) as session:
        await session.execute(
            text("update integrations set last_tested_at = now() where integration_id = :id"),
            {"id": integration_id},
        )
        outcome = "allowed" if report.ok else "failed"
        await audit.record(
            session, ctx, "integration.tested", _ref(integration_id, row.name), outcome
        )
    return report


async def _endpoint_check(request: Request, endpoint: str) -> Check:
    try:
        await _vet(request, endpoint)
    except NetguardBlocked as exc:
        return Check(name="endpoint_allowed", ok=False, detail=str(exc))
    return Check(name="endpoint_allowed", ok=True, detail="endpoint passes SSRF checks")


async def _live_checks(request: Request, endpoint: str, token: SecretStr | None) -> list[Check]:
    """Two read-only calls: build info, then `up` to see how many targets are scraped."""
    prometheus = PrometheusConnector(
        endpoint,
        token,
        dev_allow=request.app.state.dev_allow,
        transport=request.app.state.http_transport,
    )
    try:
        info = await prometheus.build_info()
        targets = await prometheus.query("up")
    except (ConnectorError, NetguardBlocked) as exc:
        return [Check(name="reachable", ok=False, detail=str(exc))]
    # Untrusted strings from the customer's server: short, and only shown as text.
    version = str(info.get("version", "unknown"))[:40] if isinstance(info, dict) else "unknown"
    count = len(targets.get("result", [])) if isinstance(targets, dict) else 0
    return [
        Check(name="reachable", ok=True, detail=f"Prometheus {version}"),
        Check(name="query_up", ok=count > 0, detail=f"{count} scrape targets"),
    ]
