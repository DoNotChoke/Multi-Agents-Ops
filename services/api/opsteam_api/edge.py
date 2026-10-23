"""Outermost ASGI middleware: request id, body size cap and a bare 500 for unhandled errors."""

import json
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from ulid import ULID

from opsteam_api.problems import MEDIA_TYPE, problem_body

MAX_BODY_BYTES = 1 << 20
log = structlog.get_logger()


async def _send_problem(send: Send, status: int, code: str, request_id: str, **extra: Any) -> None:
    body = json.dumps(problem_body(status, code, request_id, **extra)).encode()
    headers = [(b"content-type", MEDIA_TYPE.encode()), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class EdgeMiddleware:
    """Give each request a server-made id, refuse bodies over the cap, hide unhandled errors."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(ULID())  # never taken from the client
        scope.setdefault("state", {})["request_id"] = request_id
        started = False

        async def send_with_id(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        # Read the whole body up front (bounded), so oversize requests never reach a handler.
        body, more = b"", True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > MAX_BODY_BYTES:
                await _send_problem(send_with_id, 413, "request.too_large", request_id)
                return
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, replay, send_with_id)
        except Exception:
            log.exception("unhandled_error")
            if not started:
                await _send_problem(
                    send_with_id, 500, "internal", request_id, detail="Internal error"
                )
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
