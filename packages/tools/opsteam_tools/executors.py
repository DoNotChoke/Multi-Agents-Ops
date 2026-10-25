"""Shared request checks, the dispatch step, and the Fake and Replay executors."""

import json
import time
from collections.abc import Mapping, Set
from pathlib import Path
from typing import Any

from opsteam_contracts.tool_request import ToolRequest
from opsteam_contracts.tool_result import ToolResult
from opsteam_core.canonical import JsonValue
from opsteam_core.context import RequestContext
from opsteam_core.errors import NetguardBlocked, OpsteamError
from opsteam_core.ids import new_id
from pydantic import BaseModel, ValidationError

from opsteam_tools.backends import IntegrationBackend, IntegrationTarget
from opsteam_tools.compress import compress_series
from opsteam_tools.connectors.prometheus import ConnectorError, ConnectorTimeout, ResponseTooLarge
from opsteam_tools.registry import TOOLS
from opsteam_tools.registry.metrics import QueryInstantArgs, QueryRangeArgs
from opsteam_tools.registry.spec import ToolSpec
from opsteam_tools.registry.templates import TemplateError


class Denied(OpsteamError):
    """The request is refused before anything runs; `code` says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.reason = code


def check_request(request: ToolRequest, agent: str) -> tuple[ToolSpec, BaseModel]:
    """Return the tool's spec and parsed arguments, or raise Denied."""
    spec = TOOLS.get(request.tool)
    if spec is None:
        raise Denied("tool.unknown", f"no tool named {request.tool!r}")
    if agent not in spec.allowed_agents:
        raise Denied("tool.not_allowed", f"agent {agent!r} may not call {spec.name}")
    try:
        args = spec.args.model_validate(request.arguments)
    except ValidationError:
        # The message names no values: arguments come from the LLM and may carry injected text.
        raise Denied("tool.invalid_arguments", "arguments do not match the tool schema") from None
    if isinstance(args, QueryRangeArgs | QueryInstantArgs) and (
        args.params.service != request.resource.service
        or args.params.environment != request.resource.environment
    ):
        raise Denied("tool.invalid_arguments", "params must name the requested resource")
    return spec, args


def result(
    status: str,
    *,
    data: dict[str, Any] | None = None,
    error: tuple[str, str] | None = None,
    started: float | None = None,
    tool_call_id: str | None = None,
) -> ToolResult:
    """Build a ToolResult; usage counts the serialized data."""
    body = json.dumps(data) if data is not None else ""
    elapsed = int((time.monotonic() - started) * 1000) if started is not None else 0
    return ToolResult.model_validate(
        {
            "schema_version": 1,
            "tool_call_id": tool_call_id or new_id("tc"),
            "status": status,
            "evidence_ids": [],
            "summary": _summary(data),
            "data": data,
            "error": {"code": error[0], "message": error[1]} if error else None,
            "usage": {
                "duration_ms": elapsed,
                "bytes": len(body),
                "rows": len((data or {}).get("alerts", [])),
                "series": len((data or {}).get("series", [])),
            },
        }
    )


async def dispatch(
    request: ToolRequest,
    agent: str,
    backend: IntegrationBackend,
    target: IntegrationTarget,
    catalog: Set[tuple[str, str]],
) -> ToolResult:
    """Check, run and wrap one request; every failure becomes a typed ToolResult status."""
    started = time.monotonic()
    try:
        spec, args = check_request(request, agent)
    except Denied as exc:
        return result("denied", error=(exc.reason, str(exc)), started=started)
    answer, _ = await run_checked(backend, spec, args, target, catalog, started)
    return answer


async def run_checked(
    backend: IntegrationBackend,
    spec: ToolSpec,
    args: BaseModel,
    target: IntegrationTarget,
    catalog: Set[tuple[str, str]],
    started: float,
    tool_call_id: str | None = None,
) -> tuple[ToolResult, JsonValue]:
    """Run already-checked arguments; returns the result and, when ok, the raw payload."""

    def failed(status: str, exc: OpsteamError) -> tuple[ToolResult, JsonValue]:
        error = (exc.code, str(exc))
        return result(status, error=error, started=started, tool_call_id=tool_call_id), None

    try:
        fetched = await backend.run(spec, args, target, catalog)
    except TemplateError as exc:
        return failed("denied", exc)
    except ConnectorTimeout as exc:
        return failed("timeout", exc)
    except ResponseTooLarge as exc:
        return failed("limit_exceeded", exc)
    except (ConnectorError, NetguardBlocked) as exc:
        return failed("error", exc)
    answer = result("ok", data=fetched.data, started=started, tool_call_id=tool_call_id)
    return answer, fetched.raw


class FakeToolExecutor:
    """Scripted results per tool name; records every call so tests can assert on what was asked."""

    def __init__(self, script: Mapping[str, ToolResult] | None = None) -> None:
        self.script = dict(script or {})
        self.calls: list[tuple[RequestContext, ToolRequest, str]] = []

    async def execute(self, ctx: RequestContext, request: ToolRequest, *, agent: str) -> ToolResult:
        self.calls.append((ctx, request, agent))
        try:
            check_request(request, agent)
        except Denied as exc:
            return result("denied", error=(exc.reason, str(exc)))
        return self.script.get(request.tool) or result("ok", data={"truncated": False})


class ReplayToolExecutor:
    """Answers metrics tools from a recorded replay file, so evals run without a cluster."""

    def __init__(self, path: Path) -> None:
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.metrics: dict[str, list[dict[str, Any]]] = {
            m["query_id"]: m["result"] for m in doc["metrics"]
        }

    async def execute(self, ctx: RequestContext, request: ToolRequest, *, agent: str) -> ToolResult:
        try:
            spec, args = check_request(request, agent)
        except Denied as exc:
            return result("denied", error=(exc.reason, str(exc)))
        if not isinstance(args, QueryRangeArgs | QueryInstantArgs):
            return result("error", error=("replay.missing", f"{spec.name} was not recorded"))
        recorded = self.metrics.get(args.query_template)
        if recorded is None:
            return result("error", error=("replay.missing", f"{args.query_template} not recorded"))
        # shortcut: replays the recorded window whatever window_seconds asks; fine for evals.
        service = args.params.service
        series = [
            s
            for s in recorded
            if s["metric"].get("service") == service
            or s["metric"].get("pod", "").startswith(f"{service}-")
        ]
        return result("ok", data=compress_series(series, spec.limits))


def _summary(data: dict[str, Any] | None) -> str | None:
    if data is None:
        return None
    if "series" in data:
        flagged = sum(1 for s in data["series"] if s["anomalies"])
        return f"{len(data['series'])} series, {flagged} with anomalies"
    if "alerts" in data:
        return f"{len(data['alerts'])} alerts"
    return None
