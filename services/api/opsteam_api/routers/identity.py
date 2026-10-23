"""Who am I, and which tenant am I working in."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from opsteam_core.context import RequestContext
from opsteam_db import Identity, tenant_session
from pydantic import BaseModel
from sqlalchemy import text

from opsteam_api.deps import TenantNotFound, get_identity, require_scope

router = APIRouter(prefix="/api/v1", tags=["identity"])


class Membership(BaseModel):
    tenant_id: str
    roles: list[str]


class Me(BaseModel):
    user_id: str
    memberships: list[Membership]


class Tenant(BaseModel):
    tenant_id: str
    name: str
    plan: str
    region: str | None
    quotas: dict[str, Any]
    retention_days: int


@router.get("/me")
async def me(identity: Annotated[Identity, Depends(get_identity)]) -> Me:
    memberships = [
        Membership(tenant_id=t, roles=list(r)) for t, r in sorted(identity.memberships.items())
    ]
    return Me(user_id=identity.user_id, memberships=memberships)


@router.get("/tenant")
async def tenant(
    request: Request, ctx: Annotated[RequestContext, Depends(require_scope("catalog:read"))]
) -> Tenant:
    async with tenant_session(request.app.state.engine, ctx) as session:
        row = (
            await session.execute(
                text("select tenant_id, name, plan, region, quotas, retention_days from tenants")
            )
        ).one_or_none()
    if row is None:
        raise TenantNotFound("tenant not found")
    return Tenant.model_validate(row._asdict())
