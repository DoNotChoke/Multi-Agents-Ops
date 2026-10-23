import pytest
from fastapi.testclient import TestClient
from opsteam_api import devseed
from opsteam_api.main import create_app
from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id
from opsteam_db import create_engine, identify, tenant_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from tests.jwt_helpers import ISSUER, Signer, verifier

pytestmark = pytest.mark.integration

SIGNER = Signer()


async def _member(engine: AsyncEngine, subject: str, roles: list[str]) -> str:
    """Create a tenant with `subject` as a member holding `roles`; return the tenant id."""
    tenant = new_id("tnt")
    user = await identify(engine, ISSUER, subject, None)
    ctx = RequestContext(
        tenant_id=tenant,
        principal=Principal(type="system", id="test"),
        roles=(),
        scopes=frozenset(),
    )
    async with tenant_session(engine, ctx) as s:
        await s.execute(
            text("insert into tenants (tenant_id, name) values (:t, 'Acme')"), {"t": tenant}
        )
        await s.execute(
            text("insert into memberships (tenant_id, user_id, roles) values (:t, :u, :r)"),
            {"t": tenant, "u": user.user_id, "r": roles},
        )
    return tenant


def _client(engine: AsyncEngine) -> TestClient:
    # TestClient runs the app on its own event loop, so it must not share pooled connections.
    own = create_engine(engine.url, poolclass=NullPool)
    return TestClient(create_app(engine=own, verifier=verifier(SIGNER)))


def _auth(subject: str, tenant: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {SIGNER.token(sub=subject)}"}
    return headers | ({"X-Tenant-Id": tenant} if tenant else {})


def test_readyz_ok_with_database(app_engine: AsyncEngine) -> None:
    with _client(app_engine) as client:
        assert client.get("/readyz").json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer not-a-jwt"}],
)
def test_me_requires_a_valid_bearer_token(app_engine: AsyncEngine, headers: dict[str, str]) -> None:
    with _client(app_engine) as client:
        resp = client.get("/api/v1/me", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["code"] == "auth.invalid_token"
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_me_lists_memberships(app_engine: AsyncEngine) -> None:
    subject = new_id("usr")
    tenant = await _member(app_engine, subject, ["viewer"])
    with _client(app_engine) as client:
        body = client.get("/api/v1/me", headers=_auth(subject)).json()
    assert body["user_id"].startswith("usr_")
    assert body["memberships"] == [{"tenant_id": tenant, "roles": ["viewer"]}]


async def test_tenant_returns_the_members_tenant(app_engine: AsyncEngine) -> None:
    subject = new_id("usr")
    tenant = await _member(app_engine, subject, ["viewer"])
    with _client(app_engine) as client:
        body = client.get("/api/v1/tenant", headers=_auth(subject, tenant)).json()
    assert (body["tenant_id"], body["name"], body["retention_days"]) == (tenant, "Acme", 30)


async def test_tenant_header_is_required(app_engine: AsyncEngine) -> None:
    subject = new_id("usr")
    await _member(app_engine, subject, ["viewer"])
    with _client(app_engine) as client:
        resp = client.get("/api/v1/tenant", headers=_auth(subject))
    assert (resp.status_code, resp.json()["code"]) == (400, "tenant.required")


async def test_other_or_unknown_tenant_is_not_found(app_engine: AsyncEngine) -> None:
    subject, stranger = new_id("usr"), new_id("usr")
    tenant = await _member(app_engine, subject, ["viewer"])
    await _member(app_engine, stranger, ["owner"])
    with _client(app_engine) as client:
        for tenant_header in (tenant, new_id("tnt"), "not-an-id", "' or 1=1 --"):
            resp = client.get("/api/v1/tenant", headers=_auth(stranger, tenant_header))
            if tenant_header == tenant:
                assert resp.status_code == 404, "stranger must not reach another tenant"
            assert (resp.status_code, resp.json()["code"]) == (404, "tenant.not_found")


async def test_unknown_role_is_forbidden(app_engine: AsyncEngine) -> None:
    subject = new_id("usr")
    tenant = await _member(app_engine, subject, ["superuser"])
    with _client(app_engine) as client:
        resp = client.get("/api/v1/tenant", headers=_auth(subject, tenant))
    assert (resp.status_code, resp.json()["code"]) == (403, "authz.unknown_role")


async def test_dev_seed_is_idempotent(app_engine: AsyncEngine) -> None:
    for _ in range(2):
        await devseed.seed(app_engine, ISSUER)
    admin = await identify(app_engine, ISSUER, "00000000-0000-4000-8000-000000000004", None)
    assert admin.memberships == {devseed.DEV_TENANT: ("admin",)}
