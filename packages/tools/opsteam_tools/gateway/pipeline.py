"""Gateway steps 1-8: whether a ToolRequest may run and with which limits; nothing runs here."""

import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from opsteam_contracts.tool_request import ToolRequest
from opsteam_core.context import RequestContext
from opsteam_db import tenant_session
from opsteam_db.secrets import SecretNotFound, SecretStore, SecretUnreadable
from opsteam_policy.client import Decision, PolicyClient
from pydantic import BaseModel, SecretStr
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from opsteam_tools.backends import IntegrationTarget
from opsteam_tools.executors import Denied, check_request
from opsteam_tools.registry.metrics import QueryRangeArgs
from opsteam_tools.registry.spec import ToolSpec

POLICY_PATH = "opsteam/tool_access/decision"


@dataclass(frozen=True)
class Quotas:
    """Per-tenant ceilings enforced in Redis."""

    calls_per_minute: int = 120
    calls_per_incident: int = 60
    concurrent_per_integration: int = 4


@dataclass(frozen=True)
class Admitted:
    """A request that passed steps 1-8, with its arguments clamped to the tightest limits."""

    spec: ToolSpec
    args: BaseModel
    integration_id: str
    target: IntegrationTarget
    catalog: frozenset[tuple[str, str]]
    decision: Decision


class Gateway:
    def __init__(
        self,
        engine: AsyncEngine,
        secrets: SecretStore,
        policy: PolicyClient,
        redis: Redis,
        quotas: Quotas = Quotas(),  # noqa: B008
    ) -> None:
        self.engine = engine
        self.secrets = secrets
        self.policy = policy
        self.redis = redis
        self.quotas = quotas

    async def admit(self, ctx: RequestContext, request: ToolRequest, *, agent: str) -> Admitted:
        """Run steps 1-8 for `ctx`'s tenant or raise Denied; identity never comes from `request`."""
        spec, args = check_request(request, agent)  # 1 shape, 3 agent allowlist
        integration = await self._integration(ctx, request.integration_id, spec)  # 4
        catalog = await self._catalog(ctx)  # 5
        resource = (request.resource.service, request.resource.environment)
        if resource not in catalog:
            raise Denied("resource.not_in_catalog", "service is not in the tenant's catalog")
        decision = await self._policy(ctx, request, agent, spec, args, integration)  # 6
        await self._quota(ctx)  # 7
        target = IntegrationTarget(
            endpoint=integration["endpoint"],
            credentials=await self._credentials(ctx, integration["credentials_ref"]),
            metric_mapping=integration["config"].get("metric_mapping", {}),
        )
        return Admitted(
            spec=spec,
            args=_clamp(spec, args, decision),  # 8
            integration_id=request.integration_id,
            target=target,
            catalog=catalog,
            decision=decision,
        )

    @asynccontextmanager
    async def slot(self, ctx: RequestContext, integration_id: str) -> AsyncIterator[None]:
        """Hold one of the integration's concurrent-call slots while the call runs."""
        key = f"t:{ctx.tenant_id}:int:{integration_id}:inflight"
        try:
            inflight = await self.redis.incr(key)
            # shortcut: a crashed holder keeps its slot until expiry; use leases if that bites.
            await self.redis.expire(key, 60)
        except RedisError as exc:
            raise Denied("quota.unavailable", "quota store unavailable") from exc
        try:
            if inflight > self.quotas.concurrent_per_integration:
                raise Denied("quota.concurrency", "too many concurrent calls to this integration")
            yield
        finally:
            with contextlib.suppress(RedisError):  # the expiry above frees the slot
                await self.redis.decr(key)

    async def _integration(
        self, ctx: RequestContext, integration_id: str, spec: ToolSpec
    ) -> dict[str, Any]:
        async with tenant_session(self.engine, ctx) as session:
            row = (
                await session.execute(
                    text(
                        "select type, status, endpoint, credentials_ref, config"
                        " from integrations where integration_id = :id"
                    ),
                    {"id": integration_id},
                )
            ).one_or_none()
        if row is None:
            raise Denied("integration.not_found", "integration not found")
        if row.status != "active":
            raise Denied("integration.disabled", "integration is disabled")
        if row.type != spec.integration_type:
            raise Denied("integration.wrong_type", f"{spec.name} needs a {spec.integration_type}")
        return dict(row._mapping)

    async def _catalog(self, ctx: RequestContext) -> frozenset[tuple[str, str]]:
        async with tenant_session(self.engine, ctx) as session:
            rows = await session.execute(text("select name, environment from services"))
            return frozenset((name, environment) for name, environment in rows)

    async def _policy(
        self,
        ctx: RequestContext,
        request: ToolRequest,
        agent: str,
        spec: ToolSpec,
        args: BaseModel,
        integration: dict[str, Any],
    ) -> Decision:
        policy_input: dict[str, Any] = {
            "principal": {
                "tenant_id": ctx.tenant_id,
                "type": ctx.principal.type,
                "scopes": sorted(ctx.scopes),
            },
            "agent": agent,
            "tool": spec.name,
            "tool_spec": {
                "permission": spec.permission,
                "allowed_agents": sorted(spec.allowed_agents),
                "max_window_seconds": spec.limits.max_window_s,
            },
            # The row was read under the caller's RLS, so its tenant is the caller's.
            "integration": {
                "tenant_id": ctx.tenant_id,
                "type": integration["type"],
                "status": integration["status"],
            },
            "resource": request.resource.model_dump(),
            "args": {"window_seconds": getattr(args, "window_seconds", 0)},
        }
        if ctx.incident_id is not None:
            policy_input["incident"] = {"tenant_id": ctx.tenant_id}
        decision = await self.policy.decide(POLICY_PATH, policy_input)
        if not decision.allow:
            raise Denied("policy.denied", "policy denied: " + ", ".join(decision.reasons))
        return decision

    async def _quota(self, ctx: RequestContext) -> None:
        minute = int(time.time() // 60)
        # shortcut: fixed one-minute windows allow a 2x burst at the boundary; fine for MVP.
        counters = [(f"t:{ctx.tenant_id}:rate:tools:{minute}", 120, self.quotas.calls_per_minute)]
        if ctx.incident_id is not None:
            key = f"t:{ctx.tenant_id}:inc:{ctx.incident_id}:tool_calls"
            counters.append((key, 7 * 86400, self.quotas.calls_per_incident))
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                for key, ttl, _ in counters:
                    pipe.incr(key)
                    pipe.expire(key, ttl)
                counts = (await pipe.execute())[::2]
        except RedisError as exc:
            raise Denied("quota.unavailable", "quota store unavailable") from exc
        if counts[0] > counters[0][2]:
            raise Denied("quota.rate_limited", "tool call rate limit reached")
        if len(counts) > 1 and counts[1] > counters[1][2]:
            raise Denied("quota.incident_budget", "the incident's tool call budget is spent")

    async def _credentials(self, ctx: RequestContext, ref: str | None) -> SecretStr | None:
        if ref is None:
            return None
        try:
            return await self.secrets.get(ctx, ref)
        except (SecretNotFound, SecretUnreadable) as exc:
            raise Denied(
                "integration.unavailable", "integration credentials cannot be read"
            ) from exc


def _clamp(spec: ToolSpec, args: BaseModel, decision: Decision) -> BaseModel:
    if not isinstance(args, QueryRangeArgs):
        return args
    ceilings = [spec.limits.max_window_s, args.window_seconds]
    obligation = decision.obligations.get("max_window_seconds")
    if isinstance(obligation, int) and not isinstance(obligation, bool) and obligation > 0:
        ceilings.append(obligation)
    window = min(c for c in ceilings if c > 0)
    step = max(args.step_seconds, spec.limits.min_step_s)
    return args.model_copy(update={"window_seconds": window, "step_seconds": step})
