"""Read the tenant's audit log, newest first."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from opsteam_core.context import RequestContext
from opsteam_db import tenant_session
from pydantic import BaseModel
from sqlalchemy import text

from opsteam_api.deps import require_scope

router = APIRouter(prefix="/api/v1", tags=["audit"])


class AuditEvent(BaseModel):
    audit_id: str
    actor: dict[str, Any]
    action: str
    resource: dict[str, Any]
    outcome: str
    request_id: str | None
    at: datetime


class AuditPage(BaseModel):
    items: list[AuditEvent]
    next_cursor: str | None


@router.get("/audit")
async def list_audit(
    request: Request,
    ctx: Annotated[RequestContext, Depends(require_scope("audit:read"))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(pattern=r"^[0-9]{1,18}$")] = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
) -> AuditPage:
    async with tenant_session(request.app.state.engine, ctx) as session:
        rows = (
            (
                await session.execute(
                    text("""
                    select seq, audit_id, actor, action, resource, outcome, request_id, at
                    from audit_events
                    where (cast(:cursor as bigint) is null or seq < cast(:cursor as bigint))
                      and (cast(:action as text) is null or action = :action)
                    order by seq desc
                    limit :limit
                """),
                    {"cursor": cursor and int(cursor), "action": action, "limit": limit + 1},
                )
            )
            .mappings()
            .all()
        )
    page = rows[:limit]
    next_cursor = str(page[-1]["seq"]) if len(rows) > limit else None
    return AuditPage(
        items=[AuditEvent.model_validate(dict(r)) for r in page], next_cursor=next_cursor
    )
