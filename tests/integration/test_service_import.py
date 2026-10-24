from collections.abc import Callable
from typing import Any

import httpx
import pytest
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import api_client, auth, member, system_ctx

pytestmark = pytest.mark.integration

Handler = Callable[[httpx.Request], httpx.Response]
LABELS = [
    {"service": "checkout", "namespace": "shop"},
    {"service": "payment", "namespace": "shop"},
    {"service": "payment", "namespace": "shop"},  # duplicate
    {"service": 'evil"} or vector(1)', "namespace": "shop"},
    {"service": "x" * 200, "namespace": "shop"},
    {"service": "kube-dns"},  # no namespace label
]


def prometheus_with(labels: list[dict[str, str]]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        result = [{"metric": m, "value": [0, "1"]} for m in labels]
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    return handler


def _call(
    engine: AsyncEngine, method: str, path: str, who: tuple[str, str], handler: Handler, **kw: Any
) -> Any:
    tenant, subject = who
    with api_client(engine, prometheus=handler) as client:
        return client.request(method, f"/api/v1{path}", headers=auth(subject, tenant), **kw)


async def _setup(engine: AsyncEngine, role: str = "admin") -> tuple[tuple[str, str], str]:
    subject = new_id("usr")
    who = (await member(engine, subject, [role]), subject)
    admin = (who[0], new_id("usr"))
    await member(engine, admin[1], ["admin"], who[0])
    body = {
        "type": "prometheus",
        "name": "prom",
        "mode": "direct",
        "config": {"endpoint": "https://93.184.216.34"},
    }
    resp = _call(engine, "POST", "/integrations", admin, prometheus_with([]), json=body)
    return who, resp.json()["integration_id"]


async def test_suggestions_are_filtered_and_nothing_is_written(app_engine: AsyncEngine) -> None:
    who, iid = await _setup(app_engine)
    _call(
        app_engine, "POST", "/services", who, prometheus_with([]),
        json={"name": "payment", "environment": "shop"},
    )  # fmt: skip
    resp = _call(
        app_engine, "POST", "/services/import/prometheus", who, prometheus_with(LABELS),
        json={"integration_id": iid},
    )  # fmt: skip
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "suggestions": [
            {"name": "checkout", "environment": "shop", "exists": False},
            {"name": "payment", "environment": "shop", "exists": True},
        ],
        "truncated": False,
    }
    async with tenant_session(app_engine, system_ctx(who[0])) as s:
        assert await s.scalar(text("select count(*) from services")) == 1


async def test_suggestions_are_capped(app_engine: AsyncEngine) -> None:
    who, iid = await _setup(app_engine)
    many = [{"service": f"svc-{i:03d}", "namespace": "ns"} for i in range(250)]
    body = _call(
        app_engine, "POST", "/services/import/prometheus", who, prometheus_with(many),
        json={"integration_id": iid},
    ).json()  # fmt: skip
    assert (len(body["suggestions"]), body["truncated"]) == (200, True)


async def test_refusals(app_engine: AsyncEngine) -> None:
    viewer, iid = await _setup(app_engine, role="viewer")
    path, ok = "/services/import/prometheus", prometheus_with(LABELS)
    resp = _call(app_engine, "POST", path, viewer, ok, json={"integration_id": iid})
    assert resp.status_code == 403
    stranger, _ = await _setup(app_engine)
    resp = _call(app_engine, "POST", path, stranger, ok, json={"integration_id": iid})
    assert (resp.status_code, resp.json()["code"]) == (404, "integration.not_found")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    owner, own_iid = await _setup(app_engine)
    resp = _call(app_engine, "POST", path, owner, down, json={"integration_id": own_iid})
    assert (resp.status_code, resp.json()["code"]) == (502, "integration.unavailable")
