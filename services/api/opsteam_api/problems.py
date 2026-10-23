"""RFC 7807 problem responses and the exception handlers that produce them."""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opsteam_core.errors import OpsteamError
from starlette.exceptions import HTTPException

MEDIA_TYPE = "application/problem+json"

# Unlisted OpsteamError codes are client errors (400).
STATUS_BY_CODE = {
    "auth.invalid_token": 401,
    "authz.forbidden": 403,
    "authz.unknown_role": 403,
    "id.invalid": 422,
    "netguard.blocked": 422,
    "tenant.not_found": 404,
}


def problem_body(status: int, code: str, request_id: str, **extra: Any) -> dict[str, Any]:
    """Build a problem document; `extra` carries `detail` or other safe members."""
    title = HTTPStatus(status).phrase
    return {"type": "about:blank", "title": title, "status": status, "code": code,
            "request_id": request_id, **extra}  # fmt: skip


def _problem(request: Request, status: int, code: str, **extra: Any) -> JSONResponse:
    body = problem_body(status, code, request.state.request_id, **extra)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(body, status_code=status, media_type=MEDIA_TYPE, headers=headers)


def install(app: FastAPI) -> None:
    """Register the problem+json handlers on `app`."""

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        slug = HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_").replace("-", "_")
        return _problem(request, exc.status_code, f"http.{slug}")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Only location and message: pydantic's `input` would echo submitted values, secrets too.
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return _problem(request, 422, "request.invalid", errors=errors)

    @app.exception_handler(OpsteamError)
    async def opsteam_error(request: Request, exc: OpsteamError) -> JSONResponse:
        return _problem(request, STATUS_BY_CODE.get(exc.code, 400), exc.code, detail=str(exc))
