"""Server-sent events from the incident feed: one incident, or everything in the tenant."""

import asyncio
import json
import time
from collections import Counter
from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import StreamingResponse
from opsteam_core.authz import has_scope, scopes_for
from opsteam_core.context import RequestContext
from opsteam_core.errors import OpsteamError
from opsteam_db import tenant_session
from opsteam_incidents.state import IncidentNotFound
from sqlalchemy import text

from opsteam_api.deps import require_scope

router = APIRouter(prefix="/api/v1", tags=["streams"])

Reader = Annotated[RequestContext, Depends(require_scope("incident:read"))]
IncidentId = Annotated[str, Path(pattern=r"^inc_[0-9A-HJKMNP-TV-Z]{26}$")]
LastEventId = Annotated[str | None, Header(pattern=r"^[0-9]{1,18}$")]
SCOPE = "incident:read"
BATCH = 100
# Clients wait this long before reconnecting after the stream ends.
RETRY_MS = 3000

log = structlog.get_logger()

_FEED = text("""
    select seq, incident_id, type, ref_id, at from incident_activity
    where seq > :after and (cast(:incident as text) is null or incident_id = :incident)
    order by seq limit :limit
""")


class TooManyStreams(OpsteamError):
    code = "stream.too_many"


def _defaults(request: Request) -> None:
    state = request.app.state
    for name, value in (
        ("streams", Counter()),
        ("max_streams", 5),
        ("stream_poll_s", 1.0),
        ("stream_heartbeat_s", 15.0),
    ):
        if not hasattr(state, name):
            setattr(state, name, value)


@router.get("/stream", response_class=StreamingResponse)
async def tenant_stream(
    request: Request, ctx: Reader, last_event_id: LastEventId = None
) -> StreamingResponse:
    """Everything that happens to the tenant's incidents; starts now unless resuming."""
    after = int(last_event_id) if last_event_id is not None else None
    if after is None:
        async with tenant_session(request.app.state.engine, ctx) as session:
            after = int(
                await session.scalar(text("select coalesce(max(seq), 0) from incident_activity"))
            )
    return _respond(request, ctx, None, after)


@router.get("/incidents/{incident_id}/stream", response_class=StreamingResponse)
async def incident_stream(
    request: Request, ctx: Reader, incident_id: IncidentId, last_event_id: LastEventId = None
) -> StreamingResponse:
    """One incident's feed from the start, or after the last event the client saw."""
    async with tenant_session(request.app.state.engine, ctx) as session:
        found = await session.scalar(
            text("select 1 from incidents where incident_id = :i"), {"i": incident_id}
        )
    if found is None:
        raise IncidentNotFound("incident not found")
    return _respond(request, ctx, incident_id, int(last_event_id or 0))


def _respond(
    request: Request, ctx: RequestContext, incident_id: str | None, after: int
) -> StreamingResponse:
    _defaults(request)
    state = request.app.state
    key = (ctx.tenant_id, ctx.principal.id)
    # shortcut: counted per API process; move to Redis when the API runs as several replicas.
    if state.streams[key] >= state.max_streams:
        raise TooManyStreams("too many open streams")
    state.streams[key] += 1
    return StreamingResponse(
        _events(request, ctx, incident_id, after, key),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def _events(
    request: Request,
    ctx: RequestContext,
    incident_id: str | None,
    after: int,
    key: tuple[str, str],
) -> AsyncIterator[str]:
    state = request.app.state
    expires = request.state.token_expires_at
    beat = time.monotonic()
    try:
        yield f"retry: {RETRY_MS}\n\n"
        while time.time() < expires and not await request.is_disconnected():
            async with tenant_session(state.engine, ctx) as session:
                rows = (
                    await session.execute(
                        _FEED, {"after": after, "incident": incident_id, "limit": BATCH}
                    )
                ).all()
            for row in rows:
                after = row.seq
                data = {
                    "incident_id": row.incident_id,
                    "type": row.type,
                    "ref_id": row.ref_id,
                    "at": row.at.isoformat(),
                }
                yield f"id: {row.seq}\nevent: {row.type}\ndata: {json.dumps(data)}\n\n"
            if time.monotonic() - beat >= state.stream_heartbeat_s:
                if not await _still_allowed(request, ctx):
                    return
                beat = time.monotonic()
                yield "event: heartbeat\ndata: {}\n\n"
            if len(rows) < BATCH:
                await asyncio.sleep(state.stream_poll_s)
    finally:
        state.streams[key] -= 1


async def _still_allowed(request: Request, ctx: RequestContext) -> bool:
    """Whether the caller is still a member with the scope; roles can change mid-stream."""
    async with request.app.state.engine.connect() as conn:
        roles = await conn.scalar(
            text("select roles from app_memberships_for(:u) where tenant_id = :t"),
            {"u": ctx.principal.id, "t": ctx.tenant_id},
        )
    if roles is None or not has_scope(scopes_for(tuple(roles)), SCOPE):
        log.info("stream.revoked", tenant_id=ctx.tenant_id)
        return False
    return True
