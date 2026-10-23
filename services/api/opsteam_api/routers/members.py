"""Tenant membership management; only owners may grant, change or revoke the owner role."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Request
from opsteam_core.authz import has_scope
from opsteam_core.context import RequestContext
from opsteam_core.errors import OpsteamError
from opsteam_db import audit, tenant_session
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from opsteam_api.deps import Forbidden, require_scope

router = APIRouter(prefix="/api/v1", tags=["members"])

Role = Literal["viewer", "responder", "approver", "admin", "owner"]
UserId = Annotated[str, Path(pattern=r"^usr_[0-9A-HJKMNP-TV-Z]{26}$")]
Manager = Annotated[RequestContext, Depends(require_scope("member:manage"))]


class MemberExists(OpsteamError):
    code = "member.exists"


class MemberNotFound(OpsteamError):
    code = "member.not_found"


class UserNotFound(OpsteamError):
    """No account has this email yet; the person must sign in once first."""

    code = "user.not_found"


class LastOwner(OpsteamError):
    code = "member.last_owner"


class Member(BaseModel):
    user_id: str
    email: str | None
    roles: list[str]


class MemberList(BaseModel):
    items: list[Member]


class NewMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    roles: list[Role] = Field(min_length=1, max_length=5)


class RoleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[Role] = Field(min_length=1, max_length=5)


def _check_owner_rights(ctx: RequestContext, before: list[str], after: list[str]) -> None:
    if ("owner" in before or "owner" in after) and not has_scope(ctx.scopes, "member:manage_owner"):
        raise Forbidden("only owners may grant, change or revoke the owner role")


@router.get("/members")
async def list_members(request: Request, ctx: Manager) -> MemberList:
    async with tenant_session(request.app.state.engine, ctx) as session:
        rows = await session.execute(
            text("""
                select m.user_id, u.email, m.roles from memberships m
                join users u using (user_id) order by m.created_at, m.user_id
            """)
        )
        return MemberList(items=[Member.model_validate(dict(r)) for r in rows.mappings()])


@router.post("/members", status_code=201)
async def add_member(request: Request, ctx: Manager, body: NewMember) -> Member:
    roles: list[str] = list(dict.fromkeys(body.roles))
    _check_owner_rights(ctx, [], roles)
    async with tenant_session(request.app.state.engine, ctx) as session:
        user_id = await session.scalar(
            text("select app_user_id_by_email(:email)"), {"email": body.email}
        )
        if user_id is None:
            raise UserNotFound("no user with this email has signed in yet")
        added = await session.scalar(
            text("""
                insert into memberships (tenant_id, user_id, roles) values (:t, :u, :r)
                on conflict do nothing returning user_id
            """),
            {"t": ctx.tenant_id, "u": user_id, "r": roles},
        )
        if added is None:
            raise MemberExists("user is already a member")
        resource = {"type": "membership", "user_id": user_id, "roles": roles}
        await audit.record(session, ctx, "member.added", resource, "allowed")
    return Member(user_id=user_id, email=body.email, roles=roles)


@router.patch("/members/{user_id}")
async def change_roles(request: Request, ctx: Manager, user_id: UserId, body: RoleChange) -> Member:
    roles: list[str] = list(dict.fromkeys(body.roles))
    async with tenant_session(request.app.state.engine, ctx) as session:
        # Lock the owners first so two concurrent demotions cannot leave the tenant ownerless.
        owners = (
            await session.scalars(
                text("select user_id from memberships where 'owner' = any(roles) for update")
            )
        ).all()
        row = (
            await session.execute(
                text("""
                    select m.roles, u.email from memberships m join users u using (user_id)
                    where m.user_id = :u for update of m
                """),
                {"u": user_id},
            )
        ).one_or_none()
        if row is None:
            raise MemberNotFound("member not found")
        before = list(row.roles)
        _check_owner_rights(ctx, before, roles)
        if "owner" in before and "owner" not in roles and len(owners) == 1:
            raise LastOwner("a tenant must keep at least one owner")
        await session.execute(
            text("update memberships set roles = :r where user_id = :u"), {"r": roles, "u": user_id}
        )
        resource = {"type": "membership", "user_id": user_id, "before": before, "after": roles}
        await audit.record(session, ctx, "member.roles_changed", resource, "allowed")
    return Member(user_id=user_id, email=row.email, roles=roles)
