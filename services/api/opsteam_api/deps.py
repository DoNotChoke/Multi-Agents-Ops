"""Request dependencies that turn a bearer token and X-Tenant-Id into a RequestContext."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header, Request
from opsteam_core.authz import has_scope, scopes_for
from opsteam_core.context import Principal, RequestContext
from opsteam_core.errors import OpsteamError
from opsteam_db import Identity, identify

from opsteam_api.auth import InvalidToken


class TenantRequired(OpsteamError):
    """The request needs an X-Tenant-Id header."""

    code = "tenant.required"


class TenantNotFound(OpsteamError):
    """The tenant does not exist or the caller is not a member; the two are not distinguished."""

    code = "tenant.not_found"


class Forbidden(OpsteamError):
    """The caller's roles in this tenant do not grant the required scope."""

    code = "authz.forbidden"


async def get_identity(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Identity:
    """Verify the bearer token and return the caller's user and memberships."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise InvalidToken("missing bearer token")
    claims = await request.app.state.verifier.verify(token)
    request.state.token_expires_at = claims.expires_at
    identity: Identity = await identify(
        request.app.state.engine, claims.issuer, claims.subject, claims.email
    )
    return identity


async def get_context(
    request: Request,
    identity: Annotated[Identity, Depends(get_identity)],
    x_tenant_id: Annotated[str | None, Header()] = None,
) -> RequestContext:
    """Accept X-Tenant-Id only if the caller is a member; the context is the sole tenant source."""
    if not x_tenant_id:
        raise TenantRequired("X-Tenant-Id header is required")
    roles = identity.memberships.get(x_tenant_id)
    if roles is None:
        raise TenantNotFound("tenant not found")
    return RequestContext(
        tenant_id=x_tenant_id,
        principal=Principal(type="user", id=identity.user_id),
        roles=roles,
        scopes=scopes_for(roles),
        request_id=request.state.request_id,
    )


def require_scope(scope: str) -> Callable[[RequestContext], Awaitable[RequestContext]]:
    """Dependency factory: the tenant context, or 403 if it lacks `scope`."""

    async def check(ctx: Annotated[RequestContext, Depends(get_context)]) -> RequestContext:
        if not has_scope(ctx.scopes, scope):
            raise Forbidden(f"requires scope {scope}")
        return ctx

    return check
