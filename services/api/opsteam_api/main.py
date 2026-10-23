"""FastAPI application factory."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opsteam_core.logging import configure_logging
from opsteam_db import create_engine, ping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from opsteam_api import problems
from opsteam_api.auth import TokenVerifier, jwks_verifier
from opsteam_api.edge import MAX_BODY_BYTES, EdgeMiddleware
from opsteam_api.routers import audit, identity, members
from opsteam_api.settings import ApiSettings

__all__ = ["MAX_BODY_BYTES", "create_app"]

READY_TIMEOUT_S = 2.0


def create_app(engine: AsyncEngine | None = None, verifier: TokenVerifier | None = None) -> FastAPI:
    """Build the API; whatever is not injected is created from ApiSettings at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine, app.state.verifier = engine, verifier
        if engine is None or verifier is None:
            settings = ApiSettings()  # type: ignore[call-arg]  # values come from the environment
            configure_logging(settings.log_level)
            if engine is None:
                app.state.engine = create_engine(settings.database_url.get_secret_value())
            if verifier is None:
                app.state.verifier = jwks_verifier(
                    settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url
                )
        yield
        if engine is None:
            await app.state.engine.dispose()

    app = FastAPI(title="opsteam", version="0.1.0", lifespan=lifespan)
    problems.install(app)
    app.add_middleware(EdgeMiddleware)
    app.include_router(identity.router)
    app.include_router(audit.router)
    app.include_router(members.router)

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
