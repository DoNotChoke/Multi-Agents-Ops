"""Create the dev tenant and give each dev realm user a role in it; refuses to run in production."""

import asyncio
import json
import os
import sys

from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id
from opsteam_db import create_engine, identify, tenant_session
from opsteam_db.secrets import DbSecretStore, SecretStore
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from opsteam_api.settings import ApiSettings

DEV_TENANT = "tnt_01JDEVSEED0000000000000000"
# Fixed so deploy/lab can point its Alertmanager webhook here before the seed runs.
LAB_ALERTMANAGER = "int_01JDEVSEED00000000000000AM"
# The demo app in deploy/lab, filed the way the Prometheus import files them.
LAB_SERVICES = ("frontend", "checkout", "payment")
# Keycloak user ids fixed in deploy/compose/keycloak/opsteam-realm.json.
DEV_USERS = {
    "00000000-0000-4000-8000-000000000001": ("dev-viewer@example.com", "viewer"),
    "00000000-0000-4000-8000-000000000002": ("dev-responder@example.com", "responder"),
    "00000000-0000-4000-8000-000000000003": ("dev-approver@example.com", "approver"),
    "00000000-0000-4000-8000-000000000004": ("dev-admin@example.com", "admin"),
}


async def seed(
    engine: AsyncEngine,
    issuer: str,
    secrets: SecretStore | None = None,
    webhook_secret: str | None = None,
) -> None:
    """Idempotently create the dev tenant, its memberships and the lab Alertmanager webhook."""
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
        for name in LAB_SERVICES:
            await session.execute(
                text("""
                    insert into services (service_id, tenant_id, name, environment)
                    values (:id, :t, :n, 'shop')
                    on conflict (tenant_id, name, environment) do nothing
                """),
                {"id": new_id("svc"), "t": DEV_TENANT, "n": name},
            )
        # The lab's call graph, so alerts on neighbouring services fold into one incident.
        await session.execute(
            text("""
                insert into service_dependencies (tenant_id, service_id, depends_on_service_id)
                select :t, caller.service_id, callee.service_id
                from services caller join services callee
                  on callee.environment = caller.environment
                where caller.environment = 'shop'
                  and (caller.name, callee.name)
                      in (('frontend', 'checkout'), ('checkout', 'payment'))
                on conflict do nothing
            """),
            {"t": DEV_TENANT},
        )
        exists = await session.scalar(
            text("select 1 from integrations where integration_id = :i"), {"i": LAB_ALERTMANAGER}
        )
    if exists or secrets is None or webhook_secret is None:
        return
    ref = await secrets.put(ctx, SecretStr(webhook_secret))
    async with tenant_session(engine, ctx) as session:
        await session.execute(
            text("""
                insert into integrations (integration_id, tenant_id, type, name, mode, config,
                                          credentials_ref)
                values (:i, :t, 'alertmanager', 'lab-alertmanager', 'direct',
                        cast(:config as jsonb), :ref)
            """),
            {
                "i": LAB_ALERTMANAGER,
                "t": DEV_TENANT,
                "config": json.dumps({"collapse_replica_labels": True}),
                "ref": ref,
            },
        )


def main() -> None:
    settings = ApiSettings()  # type: ignore[call-arg]  # values come from the environment
    if settings.env == "production":
        sys.exit("dev-seed refuses to run with OPSTEAM_ENV=production")
    engine = create_engine(settings.database_url.get_secret_value())
    secrets = DbSecretStore(engine, settings.secrets_key)
    webhook = os.environ.get("OPSTEAM_LAB_WEBHOOK_SECRET")
    asyncio.run(seed(engine, settings.oidc_issuer, secrets, webhook))


if __name__ == "__main__":
    main()
