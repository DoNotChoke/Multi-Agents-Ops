"""Event Gateway HTTP intake: authenticated, size- and rate-limited webhooks written to Postgres."""

import asyncio
import contextlib
import hmac
import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from opsteam_core.context import Principal, RequestContext
from opsteam_core.events import EventPublisher, InProcessPublisher
from opsteam_core.logging import configure_logging
from opsteam_db import create_engine, ping, tenant_session
from opsteam_db.secrets import DbSecretStore, SecretNotFound, SecretStore, SecretUnreadable
from opsteam_obs.metrics import INGEST_REQUESTS
from prometheus_client import make_asgi_app
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from opsteam_ingest import relay
from opsteam_ingest.alertmanager import Payload, to_events
from opsteam_ingest.settings import IngestSettings

__all__ = ["MAX_BODY_BYTES", "create_app"]

MAX_BODY_BYTES = 1 << 20
READY_TIMEOUT_S = 2.0
INTEGRATION_ID = re.compile(r"int_[0-9A-HJKMNP-TV-Z]{26}")
PRINCIPAL = Principal(type="system", id="ingest")
# Compared against when there is no real secret, so unknown ids do the same work.
_NO_SECRET = "x" * 43

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


class Rejected(Exception):
    """A request refused with a problem response."""

    def __init__(self, status: int, code: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.headers = status, code, headers


def _problem(rejected: Rejected) -> JSONResponse:
    # No detail or request id: a 401 must not differ by why it happened.
    body = {"type": "about:blank", "status": rejected.status, "code": rejected.code}
    return JSONResponse(
        body,
        rejected.status,
        headers=rejected.headers,
        media_type="application/problem+json",
    )


async def _log_event(ctx: RequestContext, payload: dict[str, Any]) -> None:
    # TODO(T-2.3): replace with the Incident Manager's consumer.
    log.info("event.received", tenant_id=ctx.tenant_id, event_id=payload.get("event_id"))


def create_app(
    engine: AsyncEngine | None = None,
    system_engine: AsyncEngine | None = None,
    secrets: SecretStore | None = None,
    redis: Redis | None = None,
    rate_per_minute: int = 60,
    publisher: EventPublisher | None = None,
) -> FastAPI:
    """Build the ingest app; whatever is not injected is created from IngestSettings at startup.

    The outbox relay runs in the background only when a publisher is given or built here.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = app.state
        state.engine, state.system_engine, state.secrets, state.redis = (
            engine,
            system_engine,
            secrets,
            redis,
        )
        state.rate_per_minute = rate_per_minute
        if None in (engine, system_engine, secrets, redis):
            settings = IngestSettings()  # type: ignore[call-arg]  # values come from the environment
            configure_logging(settings.log_level)
            state.rate_per_minute = settings.webhook_rate_per_minute
            state.engine = state.engine or create_engine(settings.database_url.get_secret_value())
            state.system_engine = state.system_engine or create_engine(
                settings.system_database_url.get_secret_value()
            )
            state.secrets = state.secrets or DbSecretStore(state.engine, settings.secrets_key)
            state.redis = state.redis or Redis.from_url(settings.redis_url.get_secret_value())
        relayed = publisher
        if relayed is None and engine is None:
            relayed = InProcessPublisher({"event.received": _log_event})
        task = asyncio.create_task(relay.run(state.system_engine, relayed)) if relayed else None
        yield
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await state.redis.aclose()
        await state.engine.dispose()
        await state.system_engine.dispose()

    app = FastAPI(title="opsteam-ingest", lifespan=lifespan, openapi_url=None)
    # shortcut: served on the public webhook port; move to an internal port before production.
    app.mount("/metrics", make_asgi_app())

    @app.post("/webhooks/alertmanager/{integration_id}")
    async def alertmanager(integration_id: str, request: Request) -> Response:
        try:
            ctx, config = await _authenticate(request, integration_id)
            await _rate_limit(request, ctx, integration_id)
            payload = await _payload(request)
            accepted, duplicates = await _store(request, ctx, integration_id, config, payload)
        except Rejected as rejected:
            INGEST_REQUESTS.labels("alertmanager", rejected.code).inc()
            return _problem(rejected)
        INGEST_REQUESTS.labels("alertmanager", "accepted").inc()
        return JSONResponse({"accepted": accepted, "duplicates": duplicates})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        try:
            async with asyncio.timeout(READY_TIMEOUT_S):
                await ping(request.app.state.engine)
        except (SQLAlchemyError, OSError, TimeoutError):
            return JSONResponse({"status": "fail"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app


async def _authenticate(
    request: Request, integration_id: str
) -> tuple[RequestContext, dict[str, Any]]:
    """Find the integration's tenant and check the bearer secret; any failure is the same 401."""
    state = request.app.state
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    target, expected = None, None
    try:
        if INTEGRATION_ID.fullmatch(integration_id):
            async with state.system_engine.connect() as conn:
                target = (
                    await conn.execute(
                        text("select * from app_webhook_target(:id)"), {"id": integration_id}
                    )
                ).one_or_none()
        if target is not None and target.status == "active" and target.credentials_ref:
            ctx = RequestContext(
                tenant_id=target.tenant_id, principal=PRINCIPAL, roles=(), scopes=frozenset()
            )
            try:
                expected = (await state.secrets.get(ctx, target.credentials_ref)).get_secret_value()
            except (SecretNotFound, SecretUnreadable):
                expected = None
    except (SQLAlchemyError, OSError) as exc:
        log.warning("webhook.lookup_failed", error=type(exc).__name__)
        raise Rejected(503, "webhook.store_unavailable") from exc
    matches = hmac.compare_digest(token.encode(), (expected or _NO_SECRET).encode())
    if not (matches and expected and scheme.lower() == "bearer") or target is None:
        raise Rejected(401, "webhook.unauthorized")
    return ctx, target.config


async def _rate_limit(request: Request, ctx: RequestContext, integration_id: str) -> None:
    now = time.time()
    key = f"t:{ctx.tenant_id}:rate:webhook:{integration_id}:{int(now // 60)}"
    # shortcut: fixed one-minute windows allow a 2x burst at the boundary; fine for MVP.
    try:
        async with request.app.state.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, 120)
            count = (await pipe.execute())[0]
    except RedisError:
        # Losing alerts is worse than an unthrottled burst, so fail open.
        log.warning("webhook.rate_limit_unavailable", integration_id=integration_id)
        return
    if count > request.app.state.rate_per_minute:
        retry = str(60 - int(now) % 60)
        raise Rejected(429, "webhook.rate_limited", {"Retry-After": retry})


async def _payload(request: Request) -> Payload:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise Rejected(413, "webhook.too_large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise Rejected(413, "webhook.too_large")
    try:
        return Payload.model_validate_json(body)
    except ValidationError as exc:
        raise Rejected(400, "webhook.invalid") from exc


async def _store(
    request: Request,
    ctx: RequestContext,
    integration_id: str,
    config: dict[str, Any],
    payload: Payload,
) -> tuple[int, int]:
    """Insert new events and their outbox rows in one transaction; return accepted, duplicates."""
    events, dropped = to_events(
        payload,
        ctx.tenant_id,
        integration_id,
        collapse=bool(config.get("collapse_replica_labels", True)),
        received_at=datetime.now(UTC),
    )
    if dropped:
        log.warning("webhook.alerts_dropped", integration_id=integration_id, dropped=dropped)
    accepted = 0
    try:
        async with tenant_session(request.app.state.engine, ctx) as session:
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
    except (SQLAlchemyError, OSError) as exc:
        # Never a 2xx before the write is durable; Alertmanager retries on 5xx.
        log.warning("webhook.store_failed", error=type(exc).__name__)
        raise Rejected(503, "webhook.store_unavailable") from exc
    return accepted, len(events) - accepted
