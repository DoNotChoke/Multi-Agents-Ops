from typing import Any

import pytest
from opsteam_core.ids import new_id
from opsteam_db import audit, identify, tenant_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import api_client, auth, member, system_ctx
from tests.jwt_helpers import ISSUER

pytestmark = pytest.mark.integration


async def _team(engine: AsyncEngine, **roles: list[str]) -> tuple[str, dict[str, str]]:
    """A tenant whose members are named by `roles`; returns the tenant and name -> subject."""
    tenant = new_id("tnt")
    subjects = {name: new_id("usr") for name in roles}
    for name, member_roles in roles.items():
        await member(engine, subjects[name], member_roles, tenant, f"{subjects[name]}@example.com")
    return tenant, subjects


async def _user_id(engine: AsyncEngine, subject: str) -> str:
    return (await identify(engine, ISSUER, subject, None)).user_id


def _call(engine: AsyncEngine, method: str, path: str, subject: str, tenant: str, **kw: Any) -> Any:
    with api_client(engine) as client:
        return client.request(method, f"/api/v1{path}", headers=auth(subject, tenant), **kw)


async def _audit_actions(engine: AsyncEngine, tenant: str) -> list[str]:
    async with tenant_session(engine, system_ctx(tenant)) as s:
        assert await audit.verify_chain(s) is None
        return list((await s.scalars(text("select action from audit_events order by seq"))).all())


async def test_list_members(app_engine: AsyncEngine) -> None:
    tenant, who = await _team(app_engine, admin=["admin"], bob=["viewer"])
    resp = _call(app_engine, "GET", "/members", who["admin"], tenant)
    assert resp.status_code == 200
    roles = {m["email"]: m["roles"] for m in resp.json()["items"]}
    assert roles == {
        f"{who['admin']}@example.com": ["admin"],
        f"{who['bob']}@example.com": ["viewer"],
    }


async def test_viewer_cannot_manage_members(app_engine: AsyncEngine) -> None:
    tenant, who = await _team(app_engine, bob=["viewer"])
    for method, path in (("GET", "/members"), ("POST", "/members")):
        resp = _call(app_engine, method, path, who["bob"], tenant, json={"email": "x", "roles": []})
        assert (resp.status_code, resp.json()["code"]) == (403, "authz.forbidden")


async def test_add_existing_user_by_email(app_engine: AsyncEngine) -> None:
    tenant, who = await _team(app_engine, admin=["admin"])
    newcomer = new_id("usr")
    await identify(app_engine, ISSUER, newcomer, f"{newcomer}@example.com")
    body = {"email": f"{newcomer}@example.com", "roles": ["responder"]}
    resp = _call(app_engine, "POST", "/members", who["admin"], tenant, json=body)
    assert resp.status_code == 201
    assert resp.json()["roles"] == ["responder"]
    again = _call(app_engine, "POST", "/members", who["admin"], tenant, json=body)
    assert (again.status_code, again.json()["code"]) == (409, "member.exists")
    assert await _audit_actions(app_engine, tenant) == ["member.added"]


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"email": "nobody@example.com", "roles": ["viewer"]}, 404, "user.not_found"),
        ({"email": "x@example.com", "roles": []}, 422, "request.invalid"),
        ({"email": "x@example.com", "roles": ["superuser"]}, 422, "request.invalid"),
        (
            {"email": "x@example.com", "roles": ["viewer"], "tenant_id": "tnt_x"},
            422,
            "request.invalid",
        ),
    ],
)
async def test_add_rejections(
    app_engine: AsyncEngine, body: dict[str, Any], status: int, code: str
) -> None:
    tenant, who = await _team(app_engine, admin=["admin"])
    resp = _call(app_engine, "POST", "/members", who["admin"], tenant, json=body)
    assert (resp.status_code, resp.json()["code"]) == (status, code)


async def test_only_owners_grant_or_revoke_owner(app_engine: AsyncEngine) -> None:
    tenant, who = await _team(app_engine, owner=["owner"], admin=["admin"], bob=["viewer"])
    bob = await _user_id(app_engine, who["bob"])
    owner = await _user_id(app_engine, who["owner"])
    for target, roles in ((bob, ["owner"]), (owner, ["viewer"])):
        resp = _call(
            app_engine, "PATCH", f"/members/{target}", who["admin"], tenant, json={"roles": roles}
        )
        assert (resp.status_code, resp.json()["code"]) == (403, "authz.forbidden")
    resp = _call(
        app_engine, "PATCH", f"/members/{bob}", who["owner"], tenant, json={"roles": ["owner"]}
    )
    assert resp.status_code == 200
    resp = _call(
        app_engine, "PATCH", f"/members/{bob}", who["admin"], tenant, json={"roles": ["admin"]}
    )
    assert resp.status_code == 403, "bob is now an owner; an admin cannot demote him"


async def test_last_owner_cannot_be_demoted(app_engine: AsyncEngine) -> None:
    tenant, who = await _team(app_engine, owner=["owner"])
    owner = await _user_id(app_engine, who["owner"])
    resp = _call(
        app_engine, "PATCH", f"/members/{owner}", who["owner"], tenant, json={"roles": ["admin"]}
    )
    assert (resp.status_code, resp.json()["code"]) == (409, "member.last_owner")


async def test_admin_changes_roles_and_it_is_audited(app_engine: AsyncEngine) -> None:
    tenant, who = await _team(app_engine, admin=["admin"], bob=["viewer"])
    bob = await _user_id(app_engine, who["bob"])
    resp = _call(
        app_engine, "PATCH", f"/members/{bob}", who["admin"], tenant, json={"roles": ["approver"]}
    )
    assert (resp.status_code, resp.json()["roles"]) == (200, ["approver"])
    assert await _audit_actions(app_engine, tenant) == ["member.roles_changed"]


async def test_member_of_another_tenant_is_not_found(app_engine: AsyncEngine) -> None:
    tenant, who = await _team(app_engine, admin=["admin"])
    _, others = await _team(app_engine, eve=["viewer"])
    eve = await _user_id(app_engine, others["eve"])
    for path in (f"/members/{eve}", "/members/not-a-user-id"):
        resp = _call(app_engine, "PATCH", path, who["admin"], tenant, json={"roles": ["viewer"]})
        assert resp.status_code in (404, 422)
    resp = _call(
        app_engine, "PATCH", f"/members/{eve}", who["admin"], tenant, json={"roles": ["viewer"]}
    )
    assert resp.json()["code"] == "member.not_found"
