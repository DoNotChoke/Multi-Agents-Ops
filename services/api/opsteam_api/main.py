"""FastAPI application factory."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opsteam_core.logging import configure_logging
from opsteam_db import create_engine, ping
from opsteam_db.secrets import DbSecretStore, SecretStore
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from opsteam_api import problems
from opsteam_api.auth import TokenVerifier, jwks_verifier
from opsteam_api.edge import MAX_BODY_BYTES, EdgeMiddleware
from opsteam_api.routers import audit, identity, incidents, integrations, members, services
from opsteam_api.settings import ApiSettings

__all__ = ["MAX_BODY_BYTES", "create_app"]

READY_TIMEOUT_S = 2.0


def create_app(
    engine: AsyncEngine | None = None,
    verifier: TokenVerifier | None = None,
    secrets: SecretStore | None = None,
    dev_allow: frozenset[str] = frozenset(),
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the API; whatever is not injected is created from ApiSettings at startup.

    `http_transport` replaces the network for outbound calls to customer systems in tests.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine, app.state.verifier, app.state.secrets = engine, verifier, secrets
        app.state.dev_allow, app.state.http_transport = dev_allow, http_transport
        app.state.ingest_url = ApiSettings.model_fields["ingest_url"].default
        if engine is None or verifier is None or secrets is None:
            settings = ApiSettings()  # type: ignore[call-arg]  # values come from the environment
            configure_logging(settings.log_level)
            app.state.dev_allow = settings.netguard_dev_allow
            app.state.ingest_url = settings.ingest_url
            if engine is None:
                app.state.engine = create_engine(settings.database_url.get_secret_value())
            if verifier is None:
                app.state.verifier = jwks_verifier(
                    settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url
                )
            if secrets is None:
                app.state.secrets = DbSecretStore(app.state.engine, settings.secrets_key)
        yield
        if engine is None:
            await app.state.engine.dispose()

    app = FastAPI(title="opsteam", version="0.1.0", lifespan=lifespan)
    problems.install(app)
    app.add_middleware(EdgeMiddleware)
    app.include_router(identity.router)
    app.include_router(audit.router)
    app.include_router(members.router)
    app.include_router(services.router)
    app.include_router(integrations.router)
    app.include_router(incidents.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        try:
            async with asyncio.timeout(READY_TIMEOUT_S):
                await ping(request.app.state.engine)
        except (SQLAlchemyError, OSError, TimeoutError):
            return JSONResponse({"status": "fail"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app
