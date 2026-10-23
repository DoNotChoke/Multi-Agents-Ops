"""Shared helpers for API integration tests: members, an app client and auth headers."""

import base64
import os

from fastapi.testclient import TestClient
from opsteam_api.main import create_app
from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id
from opsteam_db import create_engine, identify, tenant_session
from opsteam_db.secrets import DbSecretStore
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from tests.jwt_helpers import ISSUER, Signer, verifier

SIGNER = Signer()
SECRETS_KEY = SecretStr(base64.b64encode(os.urandom(32)).decode())


def system_ctx(tenant: str) -> RequestContext:
    return RequestContext(
        tenant_id=tenant,
        principal=Principal(type="system", id="test"),
        roles=(),
        scopes=frozenset(),
    )


async def member(
    engine: AsyncEngine,
    subject: str,
    roles: list[str],
    tenant: str | None = None,
    email: str | None = None,
) -> str:
    """Make `subject` a member with `roles` of `tenant` (a new one if None); return the tenant."""
    tenant = tenant or new_id("tnt")
    user = await identify(engine, ISSUER, subject, email)
    async with tenant_session(engine, system_ctx(tenant)) as s:
        await s.execute(
            text(
                "insert into tenants (tenant_id, name) values (:t, 'Acme') on conflict do nothing"
            ),
            {"t": tenant},
        )
        await s.execute(
            text("insert into memberships (tenant_id, user_id, roles) values (:t, :u, :r)"),
            {"t": tenant, "u": user.user_id, "r": roles},
        )
    return tenant


def api_client(engine: AsyncEngine, dev_allow: frozenset[str] = frozenset()) -> TestClient:
    # TestClient runs the app on its own event loop, so it must not share pooled connections.
    own = create_engine(engine.url, poolclass=NullPool)
    secrets = DbSecretStore(own, SECRETS_KEY)
    app = create_app(engine=own, verifier=verifier(SIGNER), secrets=secrets, dev_allow=dev_allow)
    return TestClient(app)


def auth(subject: str, tenant: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {SIGNER.token(sub=subject)}"}
    return headers | ({"X-Tenant-Id": tenant} if tenant else {})
