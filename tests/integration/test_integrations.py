import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from opsteam_core.ids import new_id
from opsteam_db import audit, tenant_session
from opsteam_db.secrets import DbSecretStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import SECRETS_KEY, api_client, auth, fake_prometheus, member, system_ctx

pytestmark = pytest.mark.integration

FAKE_TOKEN = "dev-only-prometheus-bearer-xyz"
PUBLIC = "https://93.184.216.34"  # an IP literal, so no DNS lookup happens in tests
LAB = "http://127.0.0.1:30090"
LAB_MAPPING = json.loads(Path("deploy/lab/metric_mapping.json").read_text(encoding="utf-8"))


async def _who(engine: AsyncEngine, role: str = "admin") -> tuple[str, str]:
    subject = new_id("usr")
    return await member(engine, subject, [role]), subject


def _call(
    engine: AsyncEngine,
    method: str,
    path: str,
    who: tuple[str, str],
    prometheus: Callable[[httpx.Request], httpx.Response] = fake_prometheus,
    **kw: Any,
) -> Any:
    tenant, subject = who
    with api_client(engine, frozenset({"127.0.0.1:30090"}), prometheus) as client:
        return client.request(method, f"/api/v1{path}", headers=auth(subject, tenant), **kw)


def _body(name: str = "prom", endpoint: str = PUBLIC, **extra: Any) -> dict[str, Any]:
    return {
        "type": "prometheus",
        "name": name,
        "mode": "direct",
        "config": {"endpoint": endpoint},
    } | extra


async def test_create_never_returns_credentials(app_engine: AsyncEngine) -> None:
    who = await _who(app_engine)
    resp = _call(app_engine, "POST", "/integrations", who, json=_body(credentials=FAKE_TOKEN))
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["has_credentials"] is True
    iid = created["integration_id"]
    for r in (
        resp,
        _call(app_engine, "GET", "/integrations", who),
        _call(app_engine, "GET", f"/integrations/{iid}", who),
        _call(app_engine, "GET", "/audit", who),
    ):
        assert FAKE_TOKEN not in r.text and "credentials_ref" not in r.text and "sec_" not in r.text
    async with tenant_session(app_engine, system_ctx(who[0])) as s:
        assert await audit.verify_chain(s) is None
        row = (await s.execute(text("select credentials_ref from integrations"))).one()
        assert row.credentials_ref.startswith("sec_")
        assert FAKE_TOKEN.encode() not in await s.scalar(text("select ciphertext from secrets"))


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://169.254.169.254",
        "https://127.0.0.1",
        "https://10.0.0.5",
        "http://93.184.216.34",  # plain http
        "https://user:pw@93.184.216.34",
        "https://93.184.216.34:22",
        "http://127.0.0.1:30091",  # next to the dev-allowed port
        "file:///etc/passwd",
    ],
)
async def test_unsafe_endpoints_are_refused(app_engine: AsyncEngine, endpoint: str) -> None:
    resp = _call(
        app_engine, "POST", "/integrations", await _who(app_engine), json=_body(endpoint=endpoint)
    )
    assert (resp.status_code, resp.json()["code"]) == (422, "netguard.blocked")


async def test_lab_endpoint_is_allowed_in_dev(app_engine: AsyncEngine) -> None:
    resp = _call(
        app_engine, "POST", "/integrations", await _who(app_engine), json=_body(endpoint=LAB)
    )
    assert resp.status_code == 201, resp.text


async def test_patch_rechecks_endpoint_and_cannot_set_credentials(app_engine: AsyncEngine) -> None:
    who = await _who(app_engine)
    iid = _call(app_engine, "POST", "/integrations", who, json=_body()).json()["integration_id"]
    bad = {"config": {"endpoint": "https://169.254.169.254"}}
    resp = _call(app_engine, "PATCH", f"/integrations/{iid}", who, json=bad)
    assert (resp.status_code, resp.json()["code"]) == (422, "netguard.blocked")
    resp = _call(app_engine, "PATCH", f"/integrations/{iid}", who, json={"credentials": FAKE_TOKEN})
    assert resp.status_code == 422
    resp = _call(app_engine, "PATCH", f"/integrations/{iid}", who, json={"status": "disabled"})
    assert (resp.status_code, resp.json()["status"]) == (200, "disabled")


async def test_delete_removes_the_secret(app_engine: AsyncEngine) -> None:
    who = await _who(app_engine)
    body = _body(credentials=FAKE_TOKEN)
    iid = _call(app_engine, "POST", "/integrations", who, json=body).json()["integration_id"]
    assert _call(app_engine, "DELETE", f"/integrations/{iid}", who).status_code == 204
    assert _call(app_engine, "GET", f"/integrations/{iid}", who).status_code == 404
    async with tenant_session(app_engine, system_ctx(who[0])) as s:
        assert await s.scalar(text("select count(*) from secrets")) == 0
        actions = (await s.scalars(text("select action from audit_events order by seq"))).all()
    assert actions == ["integration.created", "integration.deleted"]


async def test_test_endpoint_calls_prometheus_with_the_stored_token(
    app_engine: AsyncEngine,
) -> None:
    who = await _who(app_engine)
    body = _body(credentials=FAKE_TOKEN)
    iid = _call(app_engine, "POST", "/integrations", who, json=body).json()["integration_id"]
    seen: list[httpx.Request] = []

    def prometheus(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return fake_prometheus(request)

    resp = _call(app_engine, "POST", f"/integrations/{iid}/test", who, prometheus)
    report = resp.json()
    assert (resp.status_code, report["ok"]) == (200, True), resp.text
    checks = {c["name"]: c["detail"] for c in report["checks"]}
    assert checks["reachable"] == "Prometheus 3.15.1"
    assert checks["query_up"] == "2 scrape targets"
    assert [r.url.path for r in seen] == ["/api/v1/status/buildinfo", "/api/v1/query"]
    assert all(r.headers["authorization"] == f"Bearer {FAKE_TOKEN}" for r in seen)
    assert {r.url.host for r in seen} == {"93.184.216.34"}
    assert FAKE_TOKEN not in resp.text


async def test_unreachable_prometheus_fails_the_test(app_engine: AsyncEngine) -> None:
    who = await _who(app_engine)
    iid = _call(app_engine, "POST", "/integrations", who, json=_body()).json()["integration_id"]

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    report = _call(app_engine, "POST", f"/integrations/{iid}/test", who, down).json()
    assert report["ok"] is False
    assert {"name": "reachable", "ok": False, "detail": "prometheus could not be reached"} in (
        report["checks"]
    )
    async with tenant_session(app_engine, system_ctx(who[0])) as s:
        outcome = await s.scalar(
            text("select outcome from audit_events where action = 'integration.tested'")
        )
    assert outcome == "failed"


async def test_permissions_and_isolation(app_engine: AsyncEngine) -> None:
    admin = await _who(app_engine)
    viewer = (admin[0], new_id("usr"))
    await member(app_engine, viewer[1], ["viewer"], admin[0])
    iid = _call(app_engine, "POST", "/integrations", admin, json=_body()).json()["integration_id"]
    assert _call(app_engine, "GET", "/integrations", viewer).status_code == 200
    for method, path in (
        ("POST", "/integrations"),
        ("PATCH", f"/integrations/{iid}"),
        ("DELETE", f"/integrations/{iid}"),
        ("POST", f"/integrations/{iid}/test"),
    ):
        resp = _call(app_engine, method, path, viewer, json=_body(name="x"))
        assert resp.status_code == 403, (method, path)
    stranger = await _who(app_engine)
    assert _call(app_engine, "GET", f"/integrations/{iid}", stranger).status_code == 404
    assert _call(app_engine, "DELETE", f"/integrations/{iid}", stranger).status_code == 404
    dup = _call(app_engine, "POST", "/integrations", admin, json=_body())
    assert (dup.status_code, dup.json()["code"]) == (409, "integration.exists")


@pytest.mark.parametrize(
    "extra",
    [
        {"mode": "runner"},
        {"type": "loki"},
        {"tenant_id": "tnt_x"},
        {"credentials": "x" * 5000},
        {"config": {"endpoint": PUBLIC, "metric_mapping": {"raw": "up"}}},
        {"config": {"endpoint": PUBLIC, "metric_mapping": {"request_rate": 'x{t="${tenant}"}'}}},
    ],
)
async def test_invalid_bodies(app_engine: AsyncEngine, extra: dict[str, Any]) -> None:
    resp = _call(app_engine, "POST", "/integrations", await _who(app_engine), json=_body() | extra)
    assert (resp.status_code, resp.json()["code"]) == (422, "request.invalid")


async def test_lab_metric_mapping_is_stored(app_engine: AsyncEngine) -> None:
    mapping = LAB_MAPPING
    body = _body(endpoint=LAB) | {"config": {"endpoint": LAB, "metric_mapping": mapping}}
    resp = _call(app_engine, "POST", "/integrations", await _who(app_engine), json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["config"]["metric_mapping"] == mapping


def _alertmanager(name: str = "am", **config: Any) -> dict[str, Any]:
    return {"type": "alertmanager", "name": name, "mode": "direct", "config": config}


async def test_alertmanager_secret_is_shown_once(app_engine: AsyncEngine) -> None:
    who = await _who(app_engine)
    resp = _call(app_engine, "POST", "/integrations", who, json=_alertmanager())
    assert resp.status_code == 201, resp.text
    created = resp.json()
    iid, secret = created["integration_id"], created["webhook_secret"]
    assert len(secret) >= 43  # 32 random bytes
    assert created["webhook_url"].endswith(f"/webhooks/alertmanager/{iid}")
    assert created["config"] == {"endpoint": None, "collapse_replica_labels": True}
    assert created["has_credentials"] is True
    for r in (
        _call(app_engine, "GET", "/integrations", who),
        _call(app_engine, "GET", f"/integrations/{iid}", who),
        _call(app_engine, "GET", "/audit", who),
        _call(app_engine, "POST", f"/integrations/{iid}/test", who),
    ):
        assert r.status_code == 200, r.text
        assert secret not in r.text and "webhook_secret" not in r.text and "sec_" not in r.text
    async with tenant_session(app_engine, system_ctx(who[0])) as s:
        ref = await s.scalar(text("select credentials_ref from integrations"))
        assert secret.encode() not in await s.scalar(text("select ciphertext from secrets"))
    store = DbSecretStore(app_engine, SECRETS_KEY)
    assert (await store.get(system_ctx(who[0]), ref)).get_secret_value() == secret


async def test_rotate_replaces_the_secret(app_engine: AsyncEngine) -> None:
    who = await _who(app_engine)
    created = _call(app_engine, "POST", "/integrations", who, json=_alertmanager()).json()
    iid = created["integration_id"]
    resp = _call(app_engine, "POST", f"/integrations/{iid}/webhook-secret/rotate", who)
    assert resp.status_code == 200, resp.text
    rotated = resp.json()
    assert rotated["webhook_secret"] != created["webhook_secret"]
    assert rotated["webhook_url"] == created["webhook_url"]
    ctx = system_ctx(who[0])
    async with tenant_session(app_engine, ctx) as s:
        ref = await s.scalar(text("select credentials_ref from integrations"))
        assert await s.scalar(text("select count(*) from secrets")) == 1  # the old one is gone
        actions = (await s.scalars(text("select action from audit_events order by seq"))).all()
        assert await audit.verify_chain(s) is None
    store = DbSecretStore(app_engine, SECRETS_KEY)
    assert (await store.get(ctx, ref)).get_secret_value() == rotated["webhook_secret"]
    assert actions == ["integration.created", "integration.secret_rotated"]


async def test_rotate_rules(app_engine: AsyncEngine) -> None:
    admin = await _who(app_engine)
    am = _call(app_engine, "POST", "/integrations", admin, json=_alertmanager()).json()
    prom = _call(app_engine, "POST", "/integrations", admin, json=_body()).json()
    viewer = (admin[0], new_id("usr"))
    await member(app_engine, viewer[1], ["viewer"], admin[0])
    path = "/integrations/{}/webhook-secret/rotate"
    assert _call(app_engine, "POST", path.format(am["integration_id"]), viewer).status_code == 403
    stranger = await _who(app_engine)
    resp = _call(app_engine, "POST", path.format(am["integration_id"]), stranger)
    assert resp.status_code == 404
    resp = _call(app_engine, "POST", path.format(prom["integration_id"]), admin)
    assert (resp.status_code, resp.json()["code"]) == (409, "integration.no_webhook")


async def test_alertmanager_endpoint_is_optional_but_vetted(app_engine: AsyncEngine) -> None:
    who = await _who(app_engine)
    body = _alertmanager(endpoint="https://169.254.169.254:9093")
    resp = _call(app_engine, "POST", "/integrations", who, json=body)
    assert (resp.status_code, resp.json()["code"]) == (422, "netguard.blocked")
    body = _alertmanager(endpoint="https://93.184.216.34:9093", collapse_replica_labels=False)
    resp = _call(app_engine, "POST", "/integrations", who, json=body)
    assert resp.status_code == 201, resp.text
    iid = resp.json()["integration_id"]
    # A Prometheus-shaped config does not fit an Alertmanager.
    bad = {"config": {"endpoint": PUBLIC, "metric_mapping": {}}}
    resp = _call(app_engine, "PATCH", f"/integrations/{iid}", who, json=bad)
    assert (resp.status_code, resp.json()["code"]) == (422, "request.invalid")
    good = {"config": {"collapse_replica_labels": True}}
    resp = _call(app_engine, "PATCH", f"/integrations/{iid}", who, json=good)
    assert resp.json()["config"] == {"endpoint": None, "collapse_replica_labels": True}


@pytest.mark.parametrize(
    "extra",
    [
        {"credentials": "dev-only-chosen-secret"},  # the server picks webhook secrets
        {"config": {"metric_mapping": {}}},
        {"mode": "runner"},
    ],
)
async def test_invalid_alertmanager_bodies(app_engine: AsyncEngine, extra: dict[str, Any]) -> None:
    body = _alertmanager() | extra
    resp = _call(app_engine, "POST", "/integrations", await _who(app_engine), json=body)
    assert (resp.status_code, resp.json()["code"]) == (422, "request.invalid")


async def test_service_import_refuses_an_alertmanager(app_engine: AsyncEngine) -> None:
    who = await _who(app_engine)
    iid = _call(app_engine, "POST", "/integrations", who, json=_alertmanager()).json()[
        "integration_id"
    ]
    body = {"integration_id": iid}
    resp = _call(app_engine, "POST", "/services/import/prometheus", who, json=body)
    assert (resp.status_code, resp.json()["code"]) == (404, "integration.not_found")
