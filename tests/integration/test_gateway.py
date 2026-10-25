import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from opsteam_contracts.tool_request import ToolRequest
from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_db.secrets import FakeSecretStore
from opsteam_policy.client import PolicyClient
from opsteam_tools.executors import Denied
from opsteam_tools.gateway.pipeline import Gateway, Quotas
from opsteam_tools.registry.metrics import QueryRangeArgs
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import system_ctx
from tests.integration.test_policy_opa import OPA_URL

pytestmark = pytest.mark.integration

REDIS_URL = f"redis://:{os.environ.get('REDIS_PASSWORD', 'dev-only-redis')}@127.0.0.1:6379/0"
Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


async def _tenant(engine: AsyncEngine, status: str = "active") -> tuple[str, str]:
    tenant, integration = new_id("tnt"), new_id("int")
    async with tenant_session(engine, system_ctx(tenant)) as s:
        await s.execute(
            text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": tenant}
        )
        await s.execute(
            text("""
                insert into integrations (integration_id, tenant_id, type, name, mode, status,
                                          endpoint)
                values (:i, :t, 'prometheus', 'p', 'direct', :s, 'https://93.184.216.34')
            """),
            {"i": integration, "t": tenant, "s": status},
        )
        await s.execute(
            text("""
                insert into services (service_id, tenant_id, name, environment)
                values (:id, :t, 'checkout', 'shop')
            """),
            {"id": new_id("svc"), "t": tenant},
        )
    return tenant, integration


def _request(integration: str, **change: Any) -> ToolRequest:
    body: dict[str, Any] = {
        "tool": "metrics.query_range",
        "integration_id": integration,
        "resource": {"service": "checkout", "environment": "shop"},
        "arguments": {
            "query_template": "http_latency_p95",
            "params": {"service": "checkout", "environment": "shop"},
            "window_seconds": 7200,
            "step_seconds": 30,
        },
    }
    return ToolRequest.model_validate(body | change)


def _answer(result: dict[str, Any] | None = None, delay: float = 0) -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(delay)
        return httpx.Response(200, json={"result": result})

    return handler


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


@pytest.fixture
def gateway(app_engine: AsyncEngine, redis: Redis) -> Callable[..., Gateway]:
    def make(policy: Handler | None = None, quotas: Quotas = Quotas()) -> Gateway:  # noqa: B008
        transport = httpx.MockTransport(policy) if policy else None
        client = PolicyClient(OPA_URL, "test", timeout=2.0, transport=transport)
        return Gateway(app_engine, FakeSecretStore(), client, redis, quotas)

    return make


async def _denied(gw: Gateway, ctx: RequestContext, request: ToolRequest, agent: str) -> str:
    with pytest.raises(Denied) as caught:
        await gw.admit(ctx, request, agent=agent)
    return caught.value.reason


async def test_valid_request_is_admitted_by_the_real_policy(
    app_engine: AsyncEngine, gateway: Callable[..., Gateway]
) -> None:
    tenant, integration = await _tenant(app_engine)
    admitted = await gateway().admit(system_ctx(tenant), _request(integration), agent="metrics")
    assert admitted.catalog == {("checkout", "shop")}
    assert admitted.target.endpoint == "https://93.184.216.34"
    assert admitted.decision.allow and admitted.decision.policy_version == "test"
    assert isinstance(admitted.args, QueryRangeArgs) and admitted.args.window_seconds == 7200


async def test_identity_never_comes_from_the_request(
    app_engine: AsyncEngine, gateway: Callable[..., Gateway]
) -> None:
    tenant, integration = await _tenant(app_engine)
    with pytest.raises(ValidationError):
        _request(integration, tenant_id=new_id("tnt"))
    smuggled = _request(integration)
    smuggled.arguments["tenant_id"] = new_id("tnt")
    assert await _denied(gateway(), system_ctx(tenant), smuggled, "metrics") == (
        "tool.invalid_arguments"
    )


async def test_step3_agent_outside_allowlist(
    app_engine: AsyncEngine, gateway: Callable[..., Gateway]
) -> None:
    tenant, integration = await _tenant(app_engine)
    reason = await _denied(gateway(), system_ctx(tenant), _request(integration), "logs")
    assert reason == "tool.not_allowed"


async def test_step4_other_tenants_or_disabled_integration(
    app_engine: AsyncEngine, gateway: Callable[..., Gateway]
) -> None:
    tenant, _ = await _tenant(app_engine)
    _, foreign = await _tenant(app_engine)
    gw = gateway()
    assert await _denied(gw, system_ctx(tenant), _request(foreign), "metrics") == (
        "integration.not_found"
    )
    off_tenant, off = await _tenant(app_engine, status="disabled")
    assert await _denied(gw, system_ctx(off_tenant), _request(off), "metrics") == (
        "integration.disabled"
    )


async def test_step5_service_outside_catalog(
    app_engine: AsyncEngine, gateway: Callable[..., Gateway]
) -> None:
    tenant, integration = await _tenant(app_engine)
    request = _request(
        integration,
        resource={"service": "payment", "environment": "shop"},
        arguments={
            "query_template": "http_latency_p95",
            "params": {"service": "payment", "environment": "shop"},
            "window_seconds": 600,
            "step_seconds": 30,
        },
    )
    reason = await _denied(gateway(), system_ctx(tenant), request, "metrics")
    assert reason == "resource.not_in_catalog"


@pytest.mark.parametrize(
    "policy",
    [
        _answer({"allow": False, "reasons": ["cross_tenant"], "obligations": {}}),
        _answer({"allow": True, "reasons": [], "obligations": {}}, delay=3),
    ],
    ids=["deny", "timeout"],
)
async def test_step6_policy_deny_or_timeout(
    app_engine: AsyncEngine, gateway: Callable[..., Gateway], policy: Handler
) -> None:
    tenant, integration = await _tenant(app_engine)
    reason = await _denied(gateway(policy), system_ctx(tenant), _request(integration), "metrics")
    assert reason == "policy.denied"


async def test_step7_rate_limit(app_engine: AsyncEngine, gateway: Callable[..., Gateway]) -> None:
    tenant, integration = await _tenant(app_engine)
    gw = gateway(quotas=Quotas(calls_per_minute=2))
    for _ in range(2):
        await gw.admit(system_ctx(tenant), _request(integration), agent="metrics")
    reason = await _denied(gw, system_ctx(tenant), _request(integration), "metrics")
    assert reason == "quota.rate_limited"
    other, other_integration = await _tenant(app_engine)
    await gw.admit(system_ctx(other), _request(other_integration), agent="metrics")


async def test_step7_incident_budget(
    app_engine: AsyncEngine, gateway: Callable[..., Gateway]
) -> None:
    tenant, integration = await _tenant(app_engine)
    ctx = RequestContext(
        tenant_id=tenant,
        principal=Principal(type="system", id="test"),
        roles=(),
        scopes=frozenset(),
        incident_id=new_id("inc"),
    )
    gw = gateway(quotas=Quotas(calls_per_incident=1))
    await gw.admit(ctx, _request(integration), agent="metrics")
    assert await _denied(gw, ctx, _request(integration), "metrics") == "quota.incident_budget"


async def test_step7_concurrency_per_integration(
    app_engine: AsyncEngine, gateway: Callable[..., Gateway]
) -> None:
    tenant, integration = await _tenant(app_engine)
    gw, ctx = gateway(quotas=Quotas(concurrent_per_integration=1)), system_ctx(tenant)
    async with gw.slot(ctx, integration):
        with pytest.raises(Denied) as caught:
            async with gw.slot(ctx, integration):
                pass
        assert caught.value.reason == "quota.concurrency"
    async with gw.slot(ctx, integration):  # released after use
        pass


async def test_step8_window_clamped_to_policy_obligation(
    app_engine: AsyncEngine, gateway: Callable[..., Gateway]
) -> None:
    tenant, integration = await _tenant(app_engine)
    policy = _answer({"allow": True, "reasons": [], "obligations": {"max_window_seconds": 3600}})
    admitted = await gateway(policy).admit(
        system_ctx(tenant), _request(integration), agent="metrics"
    )
    assert isinstance(admitted.args, QueryRangeArgs)
    assert (admitted.args.window_seconds, admitted.args.step_seconds) == (3600, 30)
