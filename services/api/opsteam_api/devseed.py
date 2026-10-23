"""Create the dev tenant and give each dev realm user a role in it; refuses to run in production."""

import asyncio
import sys

from opsteam_core.context import Principal, RequestContext
from opsteam_db import create_engine, identify, tenant_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from opsteam_api.settings import ApiSettings

DEV_TENANT = "tnt_01JDEVSEED0000000000000000"
# Keycloak user ids fixed in deploy/compose/keycloak/opsteam-realm.json.
DEV_USERS = {
    "00000000-0000-4000-8000-000000000001": ("dev-viewer@example.com", "viewer"),
    "00000000-0000-4000-8000-000000000002": ("dev-responder@example.com", "responder"),
    "00000000-0000-4000-8000-000000000003": ("dev-approver@example.com", "approver"),
    "00000000-0000-4000-8000-000000000004": ("dev-admin@example.com", "admin"),
}


async def seed(engine: AsyncEngine, issuer: str) -> None:
    """Idempotently create the dev tenant and its memberships."""
    roles_by_user = {}
    for subject, (email, role) in DEV_USERS.items():
        roles_by_user[(await identify(engine, issuer, subject, email)).user_id] = [role]
    ctx = RequestContext(
        tenant_id=DEV_TENANT,
        principal=Principal(type="system", id="dev-seed"),
        roles=(),
        scopes=frozenset(),
    )
    async with tenant_session(engine, ctx) as session:
        await session.execute(
            text(
                "insert into tenants (tenant_id, name) values (:t, 'acme-dev')"
                " on conflict do nothing"
            ),
            {"t": DEV_TENANT},
        )
        for user_id, roles in roles_by_user.items():
            await session.execute(
                text("""
                    insert into memberships (tenant_id, user_id, roles) values (:t, :u, :r)
                    on conflict (tenant_id, user_id) do update set roles = excluded.roles
                """),
                {"t": DEV_TENANT, "u": user_id, "r": roles},
            )


def main() -> None:
    settings = ApiSettings()  # type: ignore[call-arg]  # values come from the environment
    if settings.env == "production":
        sys.exit("dev-seed refuses to run with OPSTEAM_ENV=production")
    engine = create_engine(settings.database_url.get_secret_value())
    asyncio.run(seed(engine, settings.oidc_issuer))


if __name__ == "__main__":
    main()
