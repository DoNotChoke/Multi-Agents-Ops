import gzip
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from opsteam_db import tenant_session
from opsteam_db.secrets import FakeSecretStore
from opsteam_evidence.objects import FakeObjectStore
from opsteam_policy.client import PolicyClient
from opsteam_tools.backends import DirectBackend
from opsteam_tools.gateway.executor import GatewayToolExecutor
from opsteam_tools.gateway.pipeline import Gateway
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import system_ctx
from tests.integration.test_gateway import REDIS_URL, _request, _tenant
from tests.integration.test_policy_opa import OPA_URL

pytestmark = pytest.mark.integration

FAKE_TOKEN = "dev-only-label-token-7c1"


class Prometheus:
    """Counts calls; answers a range query whose label smuggles a fake credential."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls = 0
        self.fail = fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        values = [[1_792_000_000 + 30 * i, "0.2" if i < 20 else "2.4"] for i in range(60)]
        labels = {"service": "checkout", "note": f"Authorization: Bearer {FAKE_TOKEN}"}
        body = {"resultType": "matrix", "result": [{"metric": labels, "values": values}]}
        return httpx.Response(200, json={"status": "success", "data": body})


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


@pytest.fixture
def executor(
    app_engine: AsyncEngine, redis: Redis
) -> Callable[[Prometheus], tuple[GatewayToolExecutor, FakeObjectStore]]:
    def make(prometheus: Prometheus) -> tuple[GatewayToolExecutor, FakeObjectStore]:
        objects = FakeObjectStore()
        gateway = Gateway(
            app_engine, FakeSecretStore(), PolicyClient(OPA_URL, "v-test", timeout=2.0), redis
        )
        backend = DirectBackend(transport=httpx.MockTransport(prometheus))
        return GatewayToolExecutor(gateway, backend, objects), objects

    return make


async def _rows(engine: AsyncEngine, tenant: str, sql: str) -> list[dict[str, Any]]:
    async with tenant_session(engine, system_ctx(tenant)) as s:
        return [dict(r) for r in (await s.execute(text(sql))).mappings()]


async def test_ok_call_is_evidenced_recorded_and_redacted(
    app_engine: AsyncEngine, executor: Callable[..., Any]
) -> None:
    tenant, integration = await _tenant(app_engine)
    run, objects = executor(Prometheus())
    answer = await run.execute(system_ctx(tenant), _request(integration), agent="metrics")
    assert answer.status == "ok", answer.error
    assert FAKE_TOKEN not in answer.model_dump_json()
    [evidence] = await _rows(app_engine, tenant, "select * from evidence")
    assert answer.evidence_ids == [evidence["evidence_id"]]
    assert evidence["redaction_status"] == "redacted"
    stored = gzip.decompress(objects.objects[evidence["payload_reference"]][0])
    assert b'"values"' in stored and FAKE_TOKEN.encode() not in stored  # raw series, redacted
    [call] = await _rows(app_engine, tenant, "select * from tool_calls")
    assert (call["tool_call_id"], call["status"]) == (answer.tool_call_id, "ok")
    assert call["integration_id"] == integration
    assert call["policy_decision"]["policy_version"] == "v-test"
    [event] = await _rows(app_engine, tenant, "select * from audit_events")
    assert (event["action"], event["outcome"]) == ("tool.called", "allowed")
    assert event["resource"]["evidence_ids"] == answer.evidence_ids


async def test_cache_is_per_tenant(app_engine: AsyncEngine, executor: Callable[..., Any]) -> None:
    prometheus = Prometheus()
    run, _ = executor(prometheus)
    a, a_int = await _tenant(app_engine)
    b, b_int = await _tenant(app_engine)
    first = await run.execute(system_ctx(a), _request(a_int), agent="metrics")
    again = await run.execute(system_ctx(a), _request(a_int), agent="metrics")
    assert prometheus.calls == 1  # same tenant, same args: served from cache
    assert again.evidence_ids == first.evidence_ids and again.tool_call_id != first.tool_call_id
    other = await run.execute(system_ctx(b), _request(b_int), agent="metrics")
    assert prometheus.calls == 2  # another tenant never sees tenant a's entry
    assert other.evidence_ids != first.evidence_ids
    calls = await _rows(app_engine, a, "select request from tool_calls order by created_at")
    assert [c["request"]["cached"] for c in calls] == [False, True]


async def test_redaction_failure_withholds_the_payload(
    app_engine: AsyncEngine, executor: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from opsteam_core.errors import RedactionFailed
    from opsteam_evidence import store as evidence_store

    def fail(value: object) -> tuple[object, dict[str, int]]:
        raise RedactionFailed("simulated")

    monkeypatch.setattr(evidence_store, "redact_value", fail)
    tenant, integration = await _tenant(app_engine)
    run, objects = executor(Prometheus())
    answer = await run.execute(system_ctx(tenant), _request(integration), agent="metrics")
    assert (answer.status, answer.data, answer.summary) == ("error", None, None)
    assert answer.error is not None and answer.error.code == "redaction.failed"
    assert objects.objects == {} and len(answer.evidence_ids) == 1
    [event] = await _rows(app_engine, tenant, "select * from audit_events")
    assert event["outcome"] == "failed"


async def test_timeout_is_recorded_without_evidence(
    app_engine: AsyncEngine, executor: Callable[..., Any]
) -> None:
    tenant, integration = await _tenant(app_engine)
    run, _ = executor(Prometheus(fail=httpx.ReadTimeout("slow")))
    answer = await run.execute(system_ctx(tenant), _request(integration), agent="metrics")
    assert answer.status == "timeout" and answer.evidence_ids == []
    [call] = await _rows(app_engine, tenant, "select status from tool_calls")
    assert call["status"] == "timeout"
    assert await _rows(app_engine, tenant, "select * from evidence") == []


async def test_denied_call_is_recorded_too(
    app_engine: AsyncEngine, executor: Callable[..., Any]
) -> None:
    tenant, _ = await _tenant(app_engine)
    _, foreign = await _tenant(app_engine)
    prometheus = Prometheus()
    run, _ = executor(prometheus)
    answer = await run.execute(system_ctx(tenant), _request(foreign), agent="metrics")
    assert answer.status == "denied" and prometheus.calls == 0
    [call] = await _rows(app_engine, tenant, "select * from tool_calls")
    assert (call["status"], call["integration_id"]) == ("denied", None)
    assert call["request"]["integration_id"] == foreign
    [event] = await _rows(app_engine, tenant, "select outcome, resource from audit_events")
    assert event["outcome"] == "denied"
    assert event["resource"]["error"] == "integration.not_found"
    assert json.dumps(event).count(foreign) == 0  # the audit names no foreign ids
