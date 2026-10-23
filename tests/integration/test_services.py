from typing import Any

import pytest
from opsteam_core.ids import new_id
from opsteam_db import audit, tenant_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import api_client, auth, member, system_ctx

pytestmark = pytest.mark.integration


async def _admin(engine: AsyncEngine, role: str = "admin") -> tuple[str, str]:
    subject = new_id("usr")
    return await member(engine, subject, [role]), subject


def _call(engine: AsyncEngine, method: str, path: str, who: tuple[str, str], **kw: Any) -> Any:
    tenant, subject = who
    with api_client(engine) as client:
        return client.request(method, f"/api/v1{path}", headers=auth(subject, tenant), **kw)


def _create(engine: AsyncEngine, who: tuple[str, str], name: str, env: str = "prod") -> str:
    resp = _call(engine, "POST", "/services", who, json={"name": name, "environment": env})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["service_id"])


async def test_create_list_and_update(app_engine: AsyncEngine) -> None:
    who = await _admin(app_engine)
    sid = _create(app_engine, who, "checkout")
    assert sid.startswith("svc_")
    resp = _call(app_engine, "PATCH", f"/services/{sid}", who, json={"owner": "team-shop"})
    assert (resp.status_code, resp.json()["owner"]) == (200, "team-shop")
    listed = _call(app_engine, "GET", "/services", who).json()["items"]
    assert [(s["name"], s["environment"], s["owner"]) for s in listed] == [
        ("checkout", "prod", "team-shop")
    ]
    async with tenant_session(app_engine, system_ctx(who[0])) as s:
        assert await audit.verify_chain(s) is None
        actions = (await s.scalars(text("select action from audit_events order by seq"))).all()
    assert actions == ["service.created", "service.updated"]


@pytest.mark.parametrize(
    "body",
    [
        {"name": 'checkout"} or vector(1)', "environment": "prod"},
        {"name": "a" * 129, "environment": "prod"},
        {"name": "checkout", "environment": "prod\n"},
        {"name": "checkout", "environment": "prod", "tenant_id": "tnt_x"},
        {"name": "checkout", "environment": "prod", "criticality": "extreme"},
    ],
)
async def test_invalid_service_is_rejected(app_engine: AsyncEngine, body: dict[str, Any]) -> None:
    resp = _call(app_engine, "POST", "/services", await _admin(app_engine), json=body)
    assert (resp.status_code, resp.json()["code"]) == (422, "request.invalid")


async def test_duplicate_name_and_environment_conflicts(app_engine: AsyncEngine) -> None:
    who = await _admin(app_engine)
    _create(app_engine, who, "checkout")
    _create(app_engine, who, "checkout", "staging")
    resp = _call(
        app_engine, "POST", "/services", who, json={"name": "checkout", "environment": "prod"}
    )
    assert (resp.status_code, resp.json()["code"]) == (409, "service.exists")
    other = _create(app_engine, who, "payment")
    resp = _call(app_engine, "PATCH", f"/services/{other}", who, json={"name": "checkout"})
    assert (resp.status_code, resp.json()["code"]) == (409, "service.exists")


async def test_viewer_reads_but_cannot_write(app_engine: AsyncEngine) -> None:
    viewer = await _admin(app_engine, "viewer")
    assert _call(app_engine, "GET", "/services", viewer).status_code == 200
    resp = _call(app_engine, "POST", "/services", viewer, json={"name": "x", "environment": "prod"})
    assert (resp.status_code, resp.json()["code"]) == (403, "authz.forbidden")


async def test_topology(app_engine: AsyncEngine) -> None:
    who = await _admin(app_engine)
    ids = {n: _create(app_engine, who, n) for n in ("frontend", "checkout", "payment", "postgres")}
    for a, b in (("frontend", "checkout"), ("checkout", "payment"), ("checkout", "postgres")):
        body = {"depends_on_service_id": ids[b]}
        resp = _call(app_engine, "POST", f"/services/{ids[a]}/dependencies", who, json=body)
        assert resp.status_code == 201
    again = _call(
        app_engine,
        "POST",
        f"/services/{ids['frontend']}/dependencies",
        who,
        json={"depends_on_service_id": ids["checkout"]},
    )
    assert again.status_code == 201, "adding an existing edge is idempotent"
    resp = _call(
        app_engine, "DELETE", f"/services/{ids['checkout']}/dependencies/{ids['postgres']}", who
    )
    assert resp.status_code == 204
    topo = _call(app_engine, "GET", "/services/topology", who).json()
    assert {n["name"] for n in topo["nodes"]} == set(ids)
    names = {v: k for k, v in ids.items()}
    assert {(names[e["from"]], names[e["to"]]) for e in topo["edges"]} == {
        ("frontend", "checkout"),
        ("checkout", "payment"),
    }


async def test_dependency_rejections(app_engine: AsyncEngine) -> None:
    who = await _admin(app_engine)
    sid = _create(app_engine, who, "checkout")
    foreign = _create(app_engine, await _admin(app_engine), "payment")
    cases = [
        (sid, sid, 422, "service.self_dependency"),
        (sid, foreign, 404, "service.not_found"),
        (foreign, sid, 404, "service.not_found"),
    ]
    for src, dst, status, code in cases:
        resp = _call(
            app_engine,
            "POST",
            f"/services/{src}/dependencies",
            who,
            json={"depends_on_service_id": dst},
        )
        assert (resp.status_code, resp.json()["code"]) == (status, code)
    resp = _call(app_engine, "DELETE", f"/services/{sid}/dependencies/{foreign}", who)
    assert resp.status_code == 404


async def test_other_tenants_service_is_not_found(app_engine: AsyncEngine) -> None:
    who = await _admin(app_engine)
    foreign = _create(app_engine, await _admin(app_engine), "payment")
    resp = _call(app_engine, "PATCH", f"/services/{foreign}", who, json={"owner": "me"})
    assert (resp.status_code, resp.json()["code"]) == (404, "service.not_found")
    assert _call(app_engine, "GET", "/services", who).json()["items"] == []
