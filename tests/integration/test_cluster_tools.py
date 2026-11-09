import gzip
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

import httpx
import pytest
from opsteam_agents.loop import run_agent
from opsteam_agents.models import AgentTask
from opsteam_agents.specs import specialist
from opsteam_contracts.tool_request import ToolRequest
from opsteam_core.budget import BudgetCaps, MemoryBudget
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_db.secrets import FakeSecretStore
from opsteam_evidence.objects import FakeObjectStore
from opsteam_llm.fake import FakeLLM
from opsteam_llm.gateway import LLMGateway, Models, ModelSpec
from opsteam_policy.client import PolicyClient
from opsteam_tools.backends import DirectBackend
from opsteam_tools.gateway.executor import GatewayToolExecutor
from opsteam_tools.gateway.pipeline import Gateway
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import system_ctx
from tests.certs import cluster_ca
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


POD = {
    "metadata": {"name": "checkout-1", "namespace": "shop", "annotations": {"x": FAKE_PASSWORD}},
    "spec": {"containers": [{"name": "app", "env": [{"name": "DB", "value": FAKE_PASSWORD}]}]},
    "status": {"phase": "Running"},
}


async def _cluster(engine: AsyncEngine, tenant: str, secrets: FakeSecretStore) -> str:
    ref = await secrets.put(system_ctx(tenant), SecretStr("dev-only-k8s-reader-token"))
    config = json.dumps({"ca_cert": cluster_ca()})
    return await _integration(
        engine, tenant, "kubernetes", endpoint="https://93.184.216.34:6443", config=config, ref=ref
    )


async def test_k8s_answers_and_evidence_carry_no_env_or_annotations(
    app_engine: AsyncEngine, redis: Redis
) -> None:
    tenant, _ = await _tenant(app_engine)
    secrets = FakeSecretStore()
    cluster = await _cluster(app_engine, tenant, secrets)
    seen: list[httpx.Request] = []

    def api_server(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [POD]})

    run, objects = _executor(app_engine, redis, api_server, secrets)
    request = _request("k8s.get_resources", cluster, {"kind": "pods", "namespace": "shop"})
    answer = await run.execute(system_ctx(tenant), request, agent="infrastructure")
    assert answer.status == "ok", answer.error
    assert answer.summary == "1 pods"
    assert FAKE_PASSWORD not in answer.model_dump_json()
    assert seen[0].headers["authorization"] == "Bearer dev-only-k8s-reader-token"
    [evidence] = await _evidence(app_engine, tenant)
    assert evidence["source_type"] == "k8s"
    stored = gzip.decompress(objects.objects[evidence["payload_reference"]][0])
    assert b"checkout-1" in stored and FAKE_PASSWORD.encode() not in stored
    assert b'"env"' not in stored and b"annotations" not in stored


@pytest.mark.parametrize(
    ("arguments", "agent", "code"),
    [
        ({"kind": "secrets", "namespace": "shop"}, "infrastructure", "tool.invalid_arguments"),
        ({"kind": "pods", "namespace": "kube-system"}, "infrastructure", "tool.invalid_arguments"),
        ({"kind": "pods", "namespace": "shop"}, "logs", "tool.not_allowed"),
    ],
)
async def test_k8s_requests_outside_the_rules_never_reach_the_cluster(
    app_engine: AsyncEngine, redis: Redis, arguments: dict[str, Any], agent: str, code: str
) -> None:
    tenant, _ = await _tenant(app_engine)
    secrets = FakeSecretStore()
    cluster = await _cluster(app_engine, tenant, secrets)
    seen: list[httpx.Request] = []
    run, _ = _executor(app_engine, redis, lambda r: seen.append(r) or httpx.Response(500), secrets)
    request = _request("k8s.get_resources", cluster, arguments)
    answer = await run.execute(system_ctx(tenant), request, agent=agent)
    assert (answer.status, answer.error and answer.error.code) == ("denied", code)
    assert seen == []


class _Runs:
    async def started(self, ctx: Any, **fields: Any) -> None: ...

    async def finished(self, ctx: Any, result: Any, model_id: str | None) -> None: ...


@pytest.mark.parametrize(
    ("agent", "tool", "arguments"),
    [
        ("metrics", "k8s.events", {"namespace": "shop"}),
        ("logs", "k8s.get_resources", {"kind": "pods", "namespace": "shop"}),
        (
            "infrastructure",
            "logs.search",
            {"selector_template": "app_logs", "params": PARAMS, "window_seconds": 600},
        ),
    ],
)
async def test_the_gateway_refuses_another_agents_tool_even_if_a_prompt_lists_it(
    app_engine: AsyncEngine, redis: Redis, agent: str, tool: str, arguments: dict[str, Any]
) -> None:
    tenant, _ = await _tenant(app_engine)
    secrets = FakeSecretStore()
    kind = "loki" if tool.startswith("logs.") else "kubernetes"
    integration = (
        await _cluster(app_engine, tenant, secrets)
        if kind == "kubernetes"
        else await _integration(app_engine, tenant, "loki")
    )
    seen: list[httpx.Request] = []
    run, _ = _executor(app_engine, redis, lambda r: seen.append(r) or httpx.Response(500), secrets)
    # A misconfigured prompt that lists a tool the agent was never granted.
    spec = specialist(agent)
    prompt = replace(spec.prompt, allowed_tools=spec.prompt.allowed_tools | {tool})
    request = _request(tool, integration, arguments).model_dump()
    fake = FakeLLM(
        [
            {"content": {"action": "tool_call", "request": request}},
            {"content": {"action": "final", "result": {"missing_data": ["refused"]}}},
        ]
    )
    task = AgentTask.model_validate(
        {
            "goal": "look around",
            "resource": {"service": "checkout", "environment": "shop"},
            "time_window": {"start_at": "2026-11-09T08:00:00Z", "end_at": "2026-11-09T09:00:00Z"},
        }
    )
    ctx = system_ctx(tenant).model_copy(update={"incident_id": new_id("inc")})
    llm = LLMGateway(
        Models(tiers={"tier2": ["f"]}, specs={"f": ModelSpec("fake", "f", 0.0, 0.0)}),
        {"fake": fake},
        budget=MemoryBudget(BudgetCaps()),
    )
    out = await run_agent(
        ctx, replace(spec, prompt=prompt), task, tools=run, llm=llm, recorder=_Runs()
    )
    assert out.status == "completed" and seen == []
    assert "tool.not_allowed" in fake.seen[1][-1].content
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        [call] = (await s.execute(text("select tool, status, run_id from tool_calls"))).all()
    assert (call.tool, call.status, call.run_id) == (tool, "denied", out.run_id)
