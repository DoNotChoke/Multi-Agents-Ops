import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opsteam_core.ids import new_id
from opsteam_db import create_engine, tenant_session
from opsteam_db.secrets import DbSecretStore
from opsteam_ingest.app import create_app
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from tests.api_helpers import SECRETS_KEY, api_client, auth, member, system_ctx

pytestmark = pytest.mark.integration

REDIS_URL = f"redis://:{os.environ.get('REDIS_PASSWORD', 'dev-only-redis')}@127.0.0.1:6379/0"
UNAUTHORIZED = {"type": "about:blank", "status": 401, "code": "webhook.unauthorized"}


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


@contextmanager
def ingest(
    app_engine: AsyncEngine, system_engine: AsyncEngine, rate_per_minute: int = 60
) -> Iterator[TestClient]:
    # TestClient runs the app on its own event loop, so it must not share pooled connections.
    app_db = create_engine(app_engine.url, poolclass=NullPool)
    system_db = create_engine(system_engine.url, poolclass=NullPool)
    app = create_app(
        engine=app_db,
        system_engine=system_db,
        secrets=DbSecretStore(app_db, SECRETS_KEY),
        redis=Redis.from_url(REDIS_URL),
        rate_per_minute=rate_per_minute,
    )
    with TestClient(app) as client:
        yield client


async def _alertmanager(app_engine: AsyncEngine, **config: Any) -> tuple[str, str, str]:
    """Create an Alertmanager integration through the API; return tenant, id and secret."""
    subject = new_id("usr")
    tenant = await member(app_engine, subject, ["admin"])
    body = {"type": "alertmanager", "name": "am", "mode": "direct", "config": config}
    with api_client(app_engine) as api:
        created = api.post("/api/v1/integrations", json=body, headers=auth(subject, tenant)).json()
    return tenant, created["integration_id"], created["webhook_secret"]


def _alert(service: str = "checkout", **over: Any) -> dict[str, Any]:
    return {
        "status": "firing",
        "labels": {"alertname": "HighLatencyP95", "service": service, "severity": "page"},
        "annotations": {"summary": f"{service} is slow"},
        "startsAt": "2026-10-26T07:58:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://prometheus:9090/graph",
    } | over


def _payload(*alerts: dict[str, Any]) -> dict[str, Any]:
    return {"version": "4", "status": "firing", "receiver": "opsteam", "alerts": list(alerts)}


def _post(client: TestClient, iid: str, secret: str | None, **kw: Any) -> Any:
    headers = {"Authorization": f"Bearer {secret}"} if secret is not None else {}
    return client.post(f"/webhooks/alertmanager/{iid}", headers=headers, **kw)


async def _rows(app_engine: AsyncEngine, tenant: str, sql: str) -> list[Any]:
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        return list((await s.execute(text(sql))).all())


async def test_alerts_become_events_once(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    tenant, iid, secret = await _alertmanager(app_engine)
    body = _payload(_alert("checkout"), _alert("payment"))
    with ingest(app_engine, system_engine) as client:
        first = _post(client, iid, secret, json=body)
        again = _post(client, iid, secret, json=body)
    assert (first.status_code, first.json()) == (200, {"accepted": 2, "duplicates": 0})
    assert (again.status_code, again.json()) == (200, {"accepted": 0, "duplicates": 2})
    events = await _rows(app_engine, tenant, "select event_id, integration_id, title from events")
    outbox = await _rows(app_engine, tenant, "select topic, payload from outbox")
    assert sorted(e.title for e in events) == ["checkout is slow", "payment is slow"]
    assert {e.integration_id for e in events} == {iid}
    assert sorted(o.payload["event_id"] for o in outbox) == sorted(e.event_id for e in events)
    assert {o.topic for o in outbox} == {"event.received"}


async def test_replicas_of_one_alert_collapse(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    tenant, iid, secret = await _alertmanager(app_engine)
    pods = [
        _alert(labels={"alertname": "PodRestarting", "service": "checkout", "pod": p})
        for p in ("a", "b", "c")
    ]
    with ingest(app_engine, system_engine) as client:
        resp = _post(client, iid, secret, json=_payload(*pods))
    assert resp.json() == {"accepted": 1, "duplicates": 2}
    assert len(await _rows(app_engine, tenant, "select 1 from events")) == 1


async def test_every_auth_failure_looks_the_same(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    tenant, iid, secret = await _alertmanager(app_engine)
    subject = new_id("usr")
    other = await member(app_engine, subject, ["admin"])
    prom = {"type": "prometheus", "name": "p", "mode": "direct"}
    prom["config"] = {"endpoint": "https://93.184.216.34"}
    with api_client(app_engine) as api:
        prom_id = api.post("/api/v1/integrations", json=prom, headers=auth(subject, other)).json()[
            "integration_id"
        ]
    body = _payload(_alert())
    with ingest(app_engine, system_engine) as client:
        responses = [
            _post(client, iid, secret + "x", json=body),
            _post(client, iid, None, json=body),
            _post(client, iid, "", json=body),
            _post(client, new_id("int"), secret, json=body),
            _post(client, "not-an-id", secret, json=body),
            _post(client, prom_id, secret, json=body),
        ]
    for resp in responses:
        assert (resp.status_code, resp.json()) == (401, UNAUTHORIZED)
        assert secret not in resp.text
    assert await _rows(app_engine, tenant, "select 1 from events") == []


async def test_disabled_and_rotated_secrets_stop_working(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    subject = new_id("usr")
    tenant = await member(app_engine, subject, ["admin"])
    headers = auth(subject, tenant)
    body = {"type": "alertmanager", "name": "am", "mode": "direct", "config": {}}
    with api_client(app_engine) as api, ingest(app_engine, system_engine) as client:
        created = api.post("/api/v1/integrations", json=body, headers=headers).json()
        iid, old = created["integration_id"], created["webhook_secret"]
        new = api.post(f"/api/v1/integrations/{iid}/webhook-secret/rotate", headers=headers).json()
        assert _post(client, iid, old, json=_payload(_alert())).status_code == 401
        assert _post(client, iid, new["webhook_secret"], json=_payload(_alert())).status_code == 200
        api.patch(f"/api/v1/integrations/{iid}", json={"status": "disabled"}, headers=headers)
        resp = _post(client, iid, new["webhook_secret"], json=_payload(_alert("payment")))
    assert resp.status_code == 401


async def test_body_and_batch_limits(app_engine: AsyncEngine, system_engine: AsyncEngine) -> None:
    _, iid, secret = await _alertmanager(app_engine)
    many = _payload(*[_alert(f"svc-{i}") for i in range(201)])
    with ingest(app_engine, system_engine) as client:
        huge = _post(client, iid, secret, content=b"{" + b" " * (2 << 20) + b"}")
        capped = _post(client, iid, secret, json=many)
        broken = _post(client, iid, secret, content=b"{not json")
    assert (huge.status_code, huge.json()["code"]) == (413, "webhook.too_large")
    assert (capped.status_code, capped.json()) == (200, {"accepted": 200, "duplicates": 0})
    assert (broken.status_code, broken.json()["code"]) == (400, "webhook.invalid")


async def test_rate_limit(app_engine: AsyncEngine, system_engine: AsyncEngine) -> None:
    _, iid, secret = await _alertmanager(app_engine)
    with ingest(app_engine, system_engine, rate_per_minute=2) as client:
        codes = [_post(client, iid, secret, json=_payload(_alert())) for _ in range(3)]
    assert [r.status_code for r in codes] == [200, 200, 429]
    assert 1 <= int(codes[2].headers["retry-after"]) <= 60


async def test_store_failure_is_503(app_engine: AsyncEngine, system_engine: AsyncEngine) -> None:
    _, iid, secret = await _alertmanager(app_engine)
    down = create_engine(app_engine.url.set(port=1), poolclass=NullPool)  # nothing listens there
    with ingest(down, system_engine) as client:
        resp = _post(client, iid, secret, json=_payload(_alert()))
    # Alertmanager retries on 5xx, so the alert is not lost.
    assert (resp.status_code, resp.json()["code"]) == (503, "webhook.store_unavailable")


async def test_secrets_never_reach_the_database(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    tenant, iid, secret = await _alertmanager(app_engine)
    token = "glpat-" + "b" * 24
    alert = _alert(annotations={"summary": f"bad token {token}", "api_key": "dev-only-key-123"})
    alert["tenant_id"] = "tnt_01SOMEONEELSE00000000000000"  # ignored; the integration decides
    with ingest(app_engine, system_engine) as client:
        assert _post(client, iid, secret, json=_payload(alert)).status_code == 200
    [row] = await _rows(app_engine, tenant, "select tenant_id, labels, annotations from events")
    assert row.tenant_id == tenant
    assert token not in str(row) and "dev-only-key-123" not in str(row)


@pytest.mark.parametrize("table", ["integrations", "secrets", "events", "outbox", "memberships"])
async def test_system_role_only_reaches_the_lookup(system_engine: AsyncEngine, table: str) -> None:
    async with system_engine.connect() as conn:
        with pytest.raises(DBAPIError, match="permission denied"):
            await conn.execute(text(f"select * from {table}"))  # noqa: S608
