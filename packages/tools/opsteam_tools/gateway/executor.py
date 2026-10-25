"""Gateway steps 9-12: tenant-keyed cache, dispatch, evidence, and a tool_calls + audit record."""

import contextlib
import hashlib
import json
import time
from datetime import timedelta
from typing import Any

from opsteam_contracts.common import TimeWindow
from opsteam_contracts.evidence import RedactionStatus, SourceType
from opsteam_contracts.tool_request import ToolRequest
from opsteam_contracts.tool_result import Data, ToolResult
from opsteam_core.canonical import JsonValue, canonical_json
from opsteam_core.clock import Clock, SystemClock
from opsteam_core.context import RequestContext
from opsteam_core.errors import RedactionFailed
from opsteam_core.ids import new_id
from opsteam_core.redact import redact_value
from opsteam_db import audit, tenant_session
from opsteam_evidence.objects import ObjectStore
from opsteam_evidence.store import Provenance, store
from redis.exceptions import RedisError
from sqlalchemy import text

from opsteam_tools.backends import IntegrationBackend
from opsteam_tools.executors import Denied, result, run_checked
from opsteam_tools.gateway.pipeline import Admitted, Gateway
from opsteam_tools.registry.metrics import QueryInstantArgs, QueryRangeArgs

# Seconds a result stays fresh; range windows are bucketed so close-in-time calls share a hit.
CACHE_TTL = {"metrics.query_range": 60, "metrics.query_instant": 15, "alerts.list": 15}
WITHHELD = ("redaction.failed", "the result could not be redacted, so it is withheld")


class GatewayToolExecutor:
    """The ToolExecutor agents use: every call is admitted, recorded and, when ok, evidenced."""

    def __init__(
        self,
        gateway: Gateway,
        backend: IntegrationBackend,
        objects: ObjectStore,
        clock: Clock = SystemClock(),  # noqa: B008
    ) -> None:
        self.gateway = gateway
        self.backend = backend
        self.objects = objects
        self.clock = clock

    async def execute(self, ctx: RequestContext, request: ToolRequest, *, agent: str) -> ToolResult:
        call_id, started = new_id("tc"), time.monotonic()
        try:
            admitted = await self.gateway.admit(ctx, request, agent=agent)
        except Denied as exc:
            denied = result("denied", error=(exc.reason, str(exc)), tool_call_id=call_id)
            await self._record(ctx, request, None, denied, None, cached=False)
            return denied
        key = self._cache_key(ctx, admitted)
        hit = await self._cache_get(key)
        if hit is not None:
            answer = ToolResult.model_validate(hit | {"tool_call_id": call_id})
            await self._record(ctx, request, admitted, answer, None, cached=True)
            return answer
        try:
            async with self.gateway.slot(ctx, admitted.integration_id):
                answer, raw = await run_checked(
                    self.backend,
                    admitted.spec,
                    admitted.args,
                    admitted.target,
                    admitted.catalog,
                    started,
                    call_id,
                )
        except Denied as exc:
            answer = result("denied", error=(exc.reason, str(exc)), tool_call_id=call_id)
            raw = None
        if answer.status == "ok":
            answer = _redacted(answer)
        evidence_ids, withheld = await self._record(
            ctx, request, admitted, answer, raw, cached=False
        )
        if withheld:
            answer = _withheld(answer)
        answer = answer.model_copy(update={"evidence_ids": evidence_ids})
        if answer.status == "ok":
            await self._cache_set(key, admitted, answer)
        return answer

    async def _record(
        self,
        ctx: RequestContext,
        request: ToolRequest,
        admitted: Admitted | None,
        answer: ToolResult,
        raw: JsonValue,
        *,
        cached: bool,
    ) -> tuple[list[str], bool]:
        """Write tool_calls, evidence for `raw` and audit in one transaction; True if withheld."""
        logged: JsonValue = {
            "tool": request.tool,
            "integration_id": request.integration_id,
            "resource": request.resource.model_dump(),
            "arguments": admitted.args.model_dump(mode="json") if admitted else request.arguments,
            "cached": cached,
        }
        with contextlib.suppress(RedactionFailed):  # LLM-written arguments may carry pasted secrets
            logged = redact_value(logged)[0]
        decision = admitted.decision.model_dump(mode="json") if admitted else None
        evidence_ids: list[str] = list(answer.evidence_ids)
        withheld = False
        async with tenant_session(self.gateway.engine, ctx) as session:
            await session.execute(
                text("""
                    insert into tool_calls (tool_call_id, tenant_id, incident_id, tool,
                                            integration_id, request, status, duration_ms, bytes,
                                            truncated, policy_decision)
                    values (:id, :tenant, :incident, :tool, :integration, cast(:request as jsonb),
                            :status, :duration, :bytes, :truncated,
                            cast(:decision as jsonb))
                """),
                {
                    "id": answer.tool_call_id,
                    "tenant": ctx.tenant_id,
                    "incident": ctx.incident_id,
                    "tool": request.tool,
                    # Only an admitted id is surely the tenant's own; denied ids stay in request.
                    "integration": admitted.integration_id if admitted else None,
                    "request": json.dumps(logged),
                    "status": answer.status,
                    "duration": answer.usage.duration_ms,
                    "bytes": answer.usage.bytes,
                    "truncated": bool(answer.data and answer.data.truncated),
                    "decision": json.dumps(decision) if decision else None,
                },
            )
            if admitted is not None and raw is not None:
                evidence = await store(
                    session, self.objects, ctx, raw, self._provenance(ctx, admitted, answer),
                    self.clock,
                )  # fmt: skip
                evidence_ids = [evidence.evidence_id]
                withheld = evidence.redaction_status is RedactionStatus.redaction_failed
            resource: dict[str, Any] = {
                "type": "tool_call",
                "tool_call_id": answer.tool_call_id,
                "tool": request.tool,
                "status": "error" if withheld else answer.status,
                "error": WITHHELD[0] if withheld else (answer.error.code if answer.error else None),
                "policy_version": admitted.decision.policy_version if admitted else None,
                "evidence_ids": evidence_ids,
            }
            outcome = {"ok": "allowed", "denied": "denied"}.get(resource["status"], "failed")
            await audit.record(session, ctx, "tool.called", resource, outcome)
        return evidence_ids, withheld

    def _provenance(
        self, ctx: RequestContext, admitted: Admitted, answer: ToolResult
    ) -> Provenance:
        end = self.clock.now()
        args = admitted.args
        if isinstance(args, QueryRangeArgs):
            window = TimeWindow(start_at=end - timedelta(seconds=args.window_seconds), end_at=end)
        else:
            at = args.at if isinstance(args, QueryInstantArgs) and args.at else end
            window = TimeWindow(start_at=at, end_at=at)
        return Provenance(
            tool_call_id=answer.tool_call_id,
            tool=admitted.spec.name,
            source_type=SourceType.metrics,
            integration_id=admitted.integration_id,
            incident_id=ctx.incident_id,
            source_query=args.model_dump(mode="json"),
            time_window=window,
            summary=answer.summary or admitted.spec.name,
            truncated=bool(answer.data and answer.data.truncated),
        )

    def _cache_key(self, ctx: RequestContext, admitted: Admitted) -> str:
        args = canonical_json(admitted.args.model_dump(mode="json"))
        digest = hashlib.sha256(args).hexdigest()[:32]
        ttl = CACHE_TTL.get(admitted.spec.name, 15)
        bucket = int(self.clock.now().timestamp() // ttl)
        return f"t:{ctx.tenant_id}:{admitted.integration_id}:{admitted.spec.name}:{digest}:{bucket}"

    async def _cache_get(self, key: str) -> dict[str, Any] | None:
        # The cache only saves work: when Redis fails the call simply runs.
        with contextlib.suppress(RedisError, ValueError):
            cached = await self.gateway.redis.get(key)
            if cached is not None:
                loaded: dict[str, Any] = json.loads(cached)
                return loaded
        return None

    async def _cache_set(self, key: str, admitted: Admitted, answer: ToolResult) -> None:
        body = answer.model_dump(mode="json", exclude={"tool_call_id"}, exclude_none=True)
        with contextlib.suppress(RedisError):
            await self.gateway.redis.set(
                key, json.dumps(body), ex=CACHE_TTL.get(admitted.spec.name, 15)
            )


def _redacted(answer: ToolResult) -> ToolResult:
    """The agent's compressed data carries customer labels too; redact it or withhold it."""
    if answer.data is None:
        return answer
    try:
        clean, _ = redact_value(answer.data.model_dump(mode="json"))
    except RedactionFailed:
        return _withheld(answer)
    return answer.model_copy(update={"data": Data.model_validate(clean)})


def _withheld(answer: ToolResult) -> ToolResult:
    return result("error", error=WITHHELD, tool_call_id=answer.tool_call_id).model_copy(
        update={"usage": answer.usage}
    )
