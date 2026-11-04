import gzip
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from opsteam_contracts.tool_request import ToolRequest
from opsteam_core.ids import new_id
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
from tests.integration.test_gateway import REDIS_URL, _tenant
from tests.integration.test_policy_opa import OPA_URL

pytestmark = pytest.mark.integration

FAKE_PASSWORD = "dev-only-db-password-41x"
PARAMS = {"service": "checkout", "environment": "shop"}


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


async def _integration(engine: AsyncEngine, tenant: str, kind: str, **columns: Any) -> str:
    integration = new_id("int")
    async with tenant_session(engine, system_ctx(tenant)) as s:
        await s.execute(
            text("""
                insert into integrations (integration_id, tenant_id, type, name, mode, endpoint,
                                          config, credentials_ref)
                values (:i, :t, :kind, :kind, 'direct', :endpoint, cast(:config as jsonb), :ref)
            """),
            {
                "i": integration,
                "t": tenant,
                "kind": kind,
                "endpoint": columns.get("endpoint", "https://93.184.216.34"),
                "config": columns.get("config", "{}"),
                "ref": columns.get("ref"),
            },
        )
    return integration


def _executor(
    engine: AsyncEngine,
    redis: Redis,
    handler: Callable[[httpx.Request], httpx.Response],
    secrets: FakeSecretStore | None = None,
) -> tuple[GatewayToolExecutor, FakeObjectStore]:
    objects = FakeObjectStore()
    gateway = Gateway(
        engine, secrets or FakeSecretStore(), PolicyClient(OPA_URL, "v-test", timeout=2.0), redis
    )
    backend = DirectBackend(transport=httpx.MockTransport(handler))
    return GatewayToolExecutor(gateway, backend, objects), objects


def _request(tool: str, integration: str, arguments: dict[str, Any]) -> ToolRequest:
    return ToolRequest.model_validate(
        {
            "tool": tool,
            "integration_id": integration,
            "resource": {"service": "checkout", "environment": "shop"},
            "arguments": arguments,
        }
    )


async def _evidence(engine: AsyncEngine, tenant: str) -> list[dict[str, Any]]:
    async with tenant_session(engine, system_ctx(tenant)) as s:
        return [dict(r) for r in (await s.execute(text("select * from evidence"))).mappings()]


def _loki(request: httpx.Request) -> httpx.Response:
    line = (
        f'{{"level":"ERROR","msg":"db connect failed","dsn":"postgres://app:{FAKE_PASSWORD}@db"}}'
    )
    streams = [{"stream": {"pod": "checkout-1"}, "values": [["1793814454440999092", line]]}]
    body = {"status": "success", "data": {"resultType": "streams", "result": streams}}
    return httpx.Response(200, json=body)


async def test_a_secret_in_a_log_line_never_reaches_the_agent_or_evidence(
    app_engine: AsyncEngine, redis: Redis
) -> None:
    tenant, _ = await _tenant(app_engine)
    loki = await _integration(app_engine, tenant, "loki")
    run, objects = _executor(app_engine, redis, _loki)
    arguments = {"selector_template": "error_logs", "params": PARAMS, "window_seconds": 900}
    answer = await run.execute(
        system_ctx(tenant), _request("logs.search", loki, arguments), agent="logs"
    )
    assert answer.status == "ok", answer.error
    assert answer.summary == "1 log lines, 1 patterns"
    assert FAKE_PASSWORD not in answer.model_dump_json()
    [evidence] = await _evidence(app_engine, tenant)
    assert (evidence["source_type"], evidence["tool"]) == ("logs", "logs.search")
    assert evidence["redaction_status"] == "redacted"
    stored = gzip.decompress(objects.objects[evidence["payload_reference"]][0])
    assert b"db connect failed" in stored and FAKE_PASSWORD.encode() not in stored


async def test_logs_tools_refuse_other_agents_and_other_integration_types(
    app_engine: AsyncEngine, redis: Redis
) -> None:
    tenant, prometheus = await _tenant(app_engine)
    loki = await _integration(app_engine, tenant, "loki")
    run, _ = _executor(app_engine, redis, _loki)
    arguments = {"selector_template": "app_logs", "params": PARAMS, "window_seconds": 900}
    denied = await run.execute(
        system_ctx(tenant), _request("logs.search", loki, arguments), agent="metrics"
    )
    assert denied.error is not None and denied.error.code == "tool.not_allowed"
    wrong = await run.execute(
        system_ctx(tenant), _request("logs.search", prometheus, arguments), agent="logs"
    )
    assert wrong.error is not None and wrong.error.code == "integration.wrong_type"
    assert await _evidence(app_engine, tenant) == []
