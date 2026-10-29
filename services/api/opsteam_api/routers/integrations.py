"""Integrations: where a tenant's telemetry lives; credentials go to the SecretStore only."""

import contextlib
import json
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from opsteam_core.clock import SystemClock
from opsteam_core.context import RequestContext
from opsteam_core.errors import NetguardBlocked, OpsteamError
from opsteam_core.ids import new_id
from opsteam_core.netguard import check_url
from opsteam_db import audit, tenant_session
from opsteam_db.secrets import SecretNotFound, SecretUnreadable
from opsteam_notify.webhook import Channel, DeliveryFailed, Message, WebhookNotifier
from opsteam_tools.connectors.prometheus import (
    PROMETHEUS_PORTS,
    ConnectorError,
    PrometheusConnector,
)
from opsteam_tools.registry.templates import TemplateError, validate_mapping
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
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
           last_heartbeat_at, created_at, credentials_ref is not null as has_credentials
    from integrations
"""


class IntegrationExists(OpsteamError):
    code = "integration.exists"


class IntegrationNotFound(OpsteamError):
    code = "integration.not_found"


class NoWebhook(OpsteamError):
    code = "integration.no_webhook"


class PrometheusConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(max_length=2048)
    # Template name -> customer metric expression; set by admins, never by the LLM.
    metric_mapping: dict[str, str] = Field(default_factory=dict, max_length=50)

    @field_validator("metric_mapping")
    @classmethod
    def _known_templates_only(cls, mapping: dict[str, str]) -> dict[str, str]:
        try:
            validate_mapping(mapping)
        except TemplateError as exc:
            raise ValueError(str(exc)) from exc
        return mapping


class AlertmanagerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Optional: only needed to read active alerts back for reconciliation.
    endpoint: str | None = Field(default=None, max_length=2048)
    collapse_replica_labels: bool = True
    # How often the Watchdog alert repeats; three missed in a row open `visibility_lost`.
    watchdog_interval_seconds: int = Field(default=300, ge=30, le=3600)


class WebhookConfig(BaseModel):
    """A notification channel: incidents are POSTed here, signed with the generated secret."""

    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(max_length=2048)


Config = PrometheusConfig | AlertmanagerConfig | WebhookConfig
CONFIGS: dict[str, type[Config]] = {
    "prometheus": PrometheusConfig,
    "alertmanager": AlertmanagerConfig,
    "webhook": WebhookConfig,
}
PORTS = {"prometheus": PROMETHEUS_PORTS, "alertmanager": frozenset({9093}), "webhook": frozenset()}
SIGNED = ("alertmanager", "webhook")


class NewPrometheus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["prometheus"]
    name: Name
    mode: Literal["direct"]
    config: PrometheusConfig
    credentials: Annotated[SecretStr, Field(max_length=4096)] | None = None


class NewAlertmanager(BaseModel):
    """The server generates the webhook secret, so there are no credentials to send."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["alertmanager"]
    name: Name
    mode: Literal["direct"]
    config: AlertmanagerConfig = Field(default_factory=AlertmanagerConfig)


class NewWebhook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["webhook"]
    name: Name
    mode: Literal["direct"]
    config: WebhookConfig


NewIntegration = Annotated[
    NewPrometheus | NewAlertmanager | NewWebhook, Field(discriminator="type")
]


class IntegrationChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    # Checked against the stored integration's type before it is saved.
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
    last_heartbeat_at: datetime | None
    created_at: datetime
    webhook_url: str | None = None


class CreatedIntegration(Integration):
    # Returned by create only; there is no way to read it back later.
    webhook_secret: str | None = None


class WebhookSecret(BaseModel):
    webhook_url: str
    webhook_secret: str


class IntegrationList(BaseModel):
    items: list[Integration]


class Check(BaseModel):
    name: str
    ok: bool
    detail: str


class TestReport(BaseModel):
    ok: bool
    checks: list[Check]


def _webhook_url(request: Request, integration_id: str) -> str:
    return f"{request.app.state.ingest_url}/webhooks/alertmanager/{integration_id}"


def _out(request: Request, row: Any) -> Integration:
    fields = dict(row._mapping)
    fields["config"] = {"endpoint": fields.pop("endpoint"), **fields["config"]}
    if fields["type"] == "alertmanager":
        fields["webhook_url"] = _webhook_url(request, fields["integration_id"])
    return Integration.model_validate(fields)


def _stored(config: Config) -> tuple[str | None, str]:
    """Split a config into the endpoint column and the jsonb settings."""
    settings = config.model_dump()
    return settings.pop("endpoint"), json.dumps(settings)


def _ref(integration_id: str, name: str) -> dict[str, Any]:
    return {"type": "integration", "integration_id": integration_id, "name": name}


async def _vet(request: Request, kind: str, endpoint: str | None) -> None:
    if endpoint is not None:
        await check_url(endpoint, extra_ports=PORTS[kind], dev_allow=request.app.state.dev_allow)


@router.get("/integrations")
async def list_integrations(request: Request, ctx: Reader) -> IntegrationList:
    async with tenant_session(request.app.state.engine, ctx) as session:
        rows = await session.execute(text(_SELECT + " order by name"))
        return IntegrationList(items=[_out(request, r) for r in rows])


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
    return _out(request, row)


@router.post("/integrations", status_code=201)
async def create_integration(
    request: Request, ctx: Writer, body: NewIntegration
) -> CreatedIntegration:
    await _vet(request, body.type, body.config.endpoint)
    store = request.app.state.secrets
    webhook_secret = None
    if isinstance(body, NewPrometheus):
        ref = await store.put(ctx, body.credentials) if body.credentials else None
    else:
        webhook_secret = secrets.token_urlsafe(32)
        ref = await store.put(ctx, SecretStr(webhook_secret))
    integration_id = new_id("int")
    endpoint, settings = _stored(body.config)
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
                                  last_tested_at, last_heartbeat_at, created_at,
                                  credentials_ref is not null as has_credentials
                    """),
                    {
                        "id": integration_id,
                        "tenant_id": ctx.tenant_id,
                        "type": body.type,
                        "name": body.name,
                        "mode": body.mode,
                        "endpoint": endpoint,
                        "config": settings,
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
            await store.delete(ctx, ref)
        raise
    created = _out(request, row).model_dump()
    return CreatedIntegration(**created, webhook_secret=webhook_secret)


@router.patch("/integrations/{integration_id}")
async def update_integration(
    request: Request, ctx: Writer, integration_id: IntegrationId, body: IntegrationChange
) -> Integration:
    params: dict[str, Any] = {"id": integration_id, "name": body.name, "status": body.status}
    params["endpoint"] = params["config"] = None
    if body.config is not None:
        kind = await _kind(request, ctx, integration_id)
        try:
            config = CONFIGS[kind].model_validate(body.config.model_dump(exclude_unset=True))
        except ValidationError as exc:
            raise RequestValidationError(exc.errors(include_url=False)) from exc
        await _vet(request, kind, config.endpoint)
        params["endpoint"], params["config"] = _stored(config)
    try:
        async with tenant_session(request.app.state.engine, ctx) as session:
            row = (
                await session.execute(
                    text("""
                        update integrations set
                          name = coalesce(:name, name),
                          status = coalesce(:status, status),
                          endpoint = case when cast(:config as jsonb) is null
                                          then endpoint else :endpoint end,
                          config = coalesce(cast(:config as jsonb), config)
                        where integration_id = :id
                        returning integration_id, type, name, mode, status, endpoint, config,
                                  last_tested_at, last_heartbeat_at, created_at,
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
    return _out(request, row)


async def _kind(request: Request, ctx: RequestContext, integration_id: str) -> str:
    async with tenant_session(request.app.state.engine, ctx) as session:
        kind: str | None = await session.scalar(
            text("select type from integrations where integration_id = :id"),
            {"id": integration_id},
        )
    if kind is None:
        raise IntegrationNotFound("integration not found")
    return kind


@router.post("/integrations/{integration_id}/webhook-secret/rotate")
async def rotate_webhook_secret(
    request: Request, ctx: Writer, integration_id: IntegrationId
) -> WebhookSecret:
    """Replace a webhook's inbound bearer or outbound signing secret; the old one stops at once."""
    store = request.app.state.secrets
    webhook_secret = secrets.token_urlsafe(32)
    ref = await store.put(ctx, SecretStr(webhook_secret))
    try:
        async with tenant_session(request.app.state.engine, ctx) as session:
            old = (
                await session.execute(
                    text(
                        "select name, type, endpoint, credentials_ref from integrations"
                        " where integration_id = :id for update"
                    ),
                    {"id": integration_id},
                )
            ).one_or_none()
            if old is None:
                raise IntegrationNotFound("integration not found")
            if old.type not in SIGNED:
                raise NoWebhook("only webhook integrations have a webhook secret")
            await session.execute(
                text("update integrations set credentials_ref = :ref where integration_id = :id"),
                {"ref": ref, "id": integration_id},
            )
            resource = _ref(integration_id, old.name)
            await audit.record(session, ctx, "integration.secret_rotated", resource, "allowed")
    except Exception:
        await store.delete(ctx, ref)
        raise
    if old.credentials_ref is not None:
        with contextlib.suppress(SecretNotFound):
            await store.delete(ctx, old.credentials_ref)
    url = old.endpoint if old.type == "webhook" else _webhook_url(request, integration_id)
    return WebhookSecret(webhook_url=url, webhook_secret=webhook_secret)


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
                    "select name, type, endpoint, credentials_ref from integrations"
                    " where integration_id = :id"
                ),
                {"id": integration_id},
            )
        ).one_or_none()
    if row is None:
        raise IntegrationNotFound("integration not found")
    checks = [await _endpoint_check(request, row.type, row.endpoint)]
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
    # TODO(T-2.6): call the Alertmanager API once its reconciliation connector exists.
    if row.type == "prometheus" and all(c.ok for c in checks):
        checks += await _live_checks(request, row.endpoint, token)
    if row.type == "webhook" and all(c.ok for c in checks):
        checks.append(await _send_sample(request, integration_id, row.endpoint, token))
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


async def _endpoint_check(request: Request, kind: str, endpoint: str | None) -> Check:
    if endpoint is None:
        return Check(name="endpoint_allowed", ok=True, detail="no endpoint, webhook only")
    try:
        await _vet(request, kind, endpoint)
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


async def _send_sample(
    request: Request, integration_id: str, endpoint: str, secret: SecretStr | None
) -> Check:
    """POST a signed sample notification, so the receiver can check its signature handling."""
    sample = Message(
        kind="test",
        incident_id="inc_test",
        title="Test notification from opsteam",
        severity="info",
        service=None,
        environment=None,
        started_at=datetime.now(UTC),
        url=f"{request.app.state.app_url}/integrations",
    )
    notifier = WebhookNotifier(
        SystemClock(),
        dev_allow=request.app.state.dev_allow,
        transport=request.app.state.http_transport,
    )
    try:
        await notifier.send(Channel(integration_id, endpoint, secret), sample)
    except (DeliveryFailed, NetguardBlocked) as exc:
        return Check(name="delivered", ok=False, detail=str(exc))
    return Check(name="delivered", ok=True, detail="webhook accepted the sample")


class IntegrationUnavailable(OpsteamError):
    code = "integration.unavailable"


async def prometheus_for(
    request: Request, ctx: RequestContext, integration_id: str
) -> PrometheusConnector:
    """A connector for one of the tenant's active integrations, with its stored credentials."""
    async with tenant_session(request.app.state.engine, ctx) as session:
        row = (
            await session.execute(
                text(
                    "select endpoint, credentials_ref, status from integrations"
                    " where integration_id = :id and type = 'prometheus'"
                ),
                {"id": integration_id},
            )
        ).one_or_none()
    if row is None:
        raise IntegrationNotFound("integration not found")
    if row.status != "active":
        raise IntegrationUnavailable("integration is disabled")
    token = None
    if row.credentials_ref is not None:
        try:
            token = await request.app.state.secrets.get(ctx, row.credentials_ref)
        except (SecretNotFound, SecretUnreadable) as exc:
            raise IntegrationUnavailable("integration credentials cannot be read") from exc
    return PrometheusConnector(
        row.endpoint,
        token,
        dev_allow=request.app.state.dev_allow,
        transport=request.app.state.http_transport,
    )
