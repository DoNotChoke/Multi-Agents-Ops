import pytest
from opsteam_api import devseed
from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id
from opsteam_db import audit, identify, tenant_session
from opsteam_db.secrets import DbSecretStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import SECRETS_KEY, api_client, auth, member, system_ctx
from tests.jwt_helpers import ISSUER

pytestmark = pytest.mark.integration


def test_readyz_ok_with_database(app_engine: AsyncEngine) -> None:
    with api_client(app_engine) as client:
        assert client.get("/readyz").json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer not-a-jwt"}],
)
def test_me_requires_a_valid_bearer_token(app_engine: AsyncEngine, headers: dict[str, str]) -> None:
    with api_client(app_engine) as client:
        resp = client.get("/api/v1/me", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["code"] == "auth.invalid_token"
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_me_lists_memberships(app_engine: AsyncEngine) -> None:
    subject = new_id("usr")
    tenant = await member(app_engine, subject, ["viewer"])
    with api_client(app_engine) as client:
        body = client.get("/api/v1/me", headers=auth(subject)).json()
    assert body["user_id"].startswith("usr_")
    assert body["memberships"] == [{"tenant_id": tenant, "roles": ["viewer"]}]


async def test_tenant_returns_the_members_tenant(app_engine: AsyncEngine) -> None:
    subject = new_id("usr")
    tenant = await member(app_engine, subject, ["viewer"])
    with api_client(app_engine) as client:
        body = client.get("/api/v1/tenant", headers=auth(subject, tenant)).json()
    assert (body["tenant_id"], body["name"], body["retention_days"]) == (tenant, "Acme", 30)


async def test_tenant_header_is_required(app_engine: AsyncEngine) -> None:
    subject = new_id("usr")
    await member(app_engine, subject, ["viewer"])
    with api_client(app_engine) as client:
        resp = client.get("/api/v1/tenant", headers=auth(subject))
    assert (resp.status_code, resp.json()["code"]) == (400, "tenant.required")


async def test_other_or_unknown_tenant_is_not_found(app_engine: AsyncEngine) -> None:
    subject, stranger = new_id("usr"), new_id("usr")
    tenant = await member(app_engine, subject, ["viewer"])
    await member(app_engine, stranger, ["owner"])
    with api_client(app_engine) as client:
        for tenant_header in (tenant, new_id("tnt"), "not-an-id", "' or 1=1 --"):
            resp = client.get("/api/v1/tenant", headers=auth(stranger, tenant_header))
            if tenant_header == tenant:
                assert resp.status_code == 404, "stranger must not reach another tenant"
            assert (resp.status_code, resp.json()["code"]) == (404, "tenant.not_found")


async def test_unknown_role_is_forbidden(app_engine: AsyncEngine) -> None:
    subject = new_id("usr")
    tenant = await member(app_engine, subject, ["superuser"])
    with api_client(app_engine) as client:
        resp = client.get("/api/v1/tenant", headers=auth(subject, tenant))
    assert (resp.status_code, resp.json()["code"]) == (403, "authz.unknown_role")


async def test_dev_seed_is_idempotent(app_engine: AsyncEngine) -> None:
    store = DbSecretStore(app_engine, SECRETS_KEY)
    for _ in range(2):
        await devseed.seed(app_engine, ISSUER, store, "dev-only-lab-webhook")
    admin = await identify(app_engine, ISSUER, "00000000-0000-4000-8000-000000000004", None)
    assert admin.memberships == {devseed.DEV_TENANT: ("admin",)}
    # The lab Alertmanager posts to a fixed integration with a known dev secret.
    ctx = system_ctx(devseed.DEV_TENANT)
    async with tenant_session(app_engine, ctx) as s:
        row = (
            await s.execute(
                text(
                    "select type, name, credentials_ref from integrations where integration_id = :i"
                ),
                {"i": devseed.LAB_ALERTMANAGER},
            )
        ).one()
        assert await s.scalar(text("select count(*) from secrets")) == 1
    assert (row.type, row.name) == ("alertmanager", "lab-alertmanager")
    assert (await store.get(ctx, row.credentials_ref)).get_secret_value() == "dev-only-lab-webhook"


EVERYONE = dict.fromkeys(["viewer", "responder", "approver", "admin", "owner"], 200)
ADMINS_ONLY = {"viewer": 403, "responder": 403, "approver": 403, "admin": 200, "owner": 200}
# Expected status per role; every new endpoint gets a row here.
SCOPE_TABLE = {
    "/api/v1/tenant": EVERYONE,
    "/api/v1/services": EVERYONE,
    "/api/v1/services/topology": EVERYONE,
    "/api/v1/integrations": EVERYONE,
    "/api/v1/audit": ADMINS_ONLY,
    "/api/v1/members": ADMINS_ONLY,
}


@pytest.mark.parametrize(
    ("path", "role"), [(p, r) for p, roles in SCOPE_TABLE.items() for r in roles]
)
async def test_scope_table(app_engine: AsyncEngine, path: str, role: str) -> None:
    subject = new_id("usr")
    tenant = await member(app_engine, subject, [role])
    with api_client(app_engine) as client:
        resp = client.get(path, headers=auth(subject, tenant))
    assert resp.status_code == SCOPE_TABLE[path][role]
    if resp.status_code == 403:
        assert resp.json()["code"] == "authz.forbidden"


async def test_audit_pages_newest_first_without_gaps(app_engine: AsyncEngine) -> None:
    subject = new_id("usr")
    tenant = await member(app_engine, subject, ["admin"])
    ctx = RequestContext(
        tenant_id=tenant,
        principal=Principal(type="system", id="test"),
        roles=(),
        scopes=frozenset(),
    )
    async with tenant_session(app_engine, ctx) as s:
        for i in range(5):
            await audit.record(s, ctx, "test.event", {"type": "n", "id": str(i)}, "allowed")
        await audit.record(s, ctx, "other.event", {"type": "n", "id": "x"}, "denied")
    seen, cursor = [], None
    with api_client(app_engine) as client:
        while True:
            params = {"limit": 2, "action": "test.event"} | ({"cursor": cursor} if cursor else {})
            body = client.get("/api/v1/audit", headers=auth(subject, tenant), params=params).json()
            seen += [item["resource"]["id"] for item in body["items"]]
            cursor = body["next_cursor"]
            if cursor is None:
                break
        bad = client.get("/api/v1/audit", headers=auth(subject, tenant), params={"cursor": "1;--"})
    assert seen == ["4", "3", "2", "1", "0"]
    assert (bad.status_code, bad.json()["code"]) == (422, "request.invalid")
