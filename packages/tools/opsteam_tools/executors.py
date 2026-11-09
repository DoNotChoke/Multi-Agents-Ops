"""Shared request checks, the dispatch step, and the Fake and Replay executors."""

import json
import time
from collections.abc import Mapping, Set
from pathlib import Path
from typing import Any

from opsteam_contracts.tool_request import ToolRequest
from opsteam_contracts.tool_result import ToolResult
from opsteam_core.canonical import JsonValue, canonical_json
from opsteam_core.context import RequestContext
from opsteam_core.errors import NetguardBlocked, OpsteamError
from opsteam_core.ids import derived_id, new_id
from pydantic import BaseModel, ValidationError

from opsteam_tools import k8s_view
from opsteam_tools.backends import IntegrationBackend, IntegrationTarget
from opsteam_tools.compress import MAX_LINE, compress_logs, compress_series, log_lines
from opsteam_tools.connectors.prometheus import ConnectorError, ConnectorTimeout, ResponseTooLarge
from opsteam_tools.registry import TOOLS
from opsteam_tools.registry.k8s import MAX_OBJECTS, K8sEventsArgs
from opsteam_tools.registry.logs import LogsAggregateArgs, LogsContextArgs, LogsSearchArgs
from opsteam_tools.registry.metrics import QueryInstantArgs, QueryRangeArgs, TemplateParams
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
    params = getattr(args, "params", None)
    if isinstance(params, TemplateParams) and (
        params.service != request.resource.service
        or params.environment != request.resource.environment
    ):
        raise Denied("tool.invalid_arguments", "params must name the requested resource")
    # Catalog environments are namespaces, so a namespace outside the resource is refused.
    namespace = getattr(args, "namespace", None)
    if isinstance(namespace, str) and namespace != request.resource.environment:
        raise Denied("tool.invalid_arguments", "namespace must be the requested environment")
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
                "rows": _rows(data or {}),
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
    """Answers tools from a recorded replay file, so agents and evals run without a cluster."""

    def __init__(self, path: Path) -> None:
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.metrics: dict[str, list[dict[str, Any]]] = {
            m["query_id"]: m["result"] for m in doc["metrics"]
        }
        self.logs: list[dict[str, Any]] = [s for q in doc.get("logs", []) for s in q["result"]]
        self.events: list[dict[str, Any]] = doc.get("k8s_events", [])

    async def execute(self, ctx: RequestContext, request: ToolRequest, *, agent: str) -> ToolResult:
        try:
            spec, args = check_request(request, agent)
        except Denied as exc:
            return result("denied", error=(exc.reason, str(exc)))
        data = self._answer(spec, args)
        if data is None:
            return result("error", error=("replay.missing", f"{spec.name} was not recorded"))
        # The same call always cites the same evidence, so scripted agents can quote it.
        evidence = derived_id("ev", canonical_json([spec.name, args.model_dump(mode="json")]))
        return result("ok", data=data).model_copy(update={"evidence_ids": [evidence]})

    def _answer(self, spec: ToolSpec, args: BaseModel) -> dict[str, Any] | None:
        # shortcut: replays the recorded window whatever window_seconds asks; fine for evals.
        if isinstance(args, QueryRangeArgs | QueryInstantArgs):
            recorded = self.metrics.get(args.query_template)
            if recorded is None:
                return None
            series = [s for s in recorded if _of_service(s.get("metric", {}), args.params.service)]
            return compress_series(series, spec.limits)
        if isinstance(args, LogsSearchArgs | LogsAggregateArgs | LogsContextArgs):
            # shortcut: only error lines are recorded, so app_logs and error_logs replay the same.
            streams = [
                s for s in self.logs if _of_service(s.get("stream", {}), args.params.service)
            ]
            if not streams:
                return None
            return _replay_logs(spec, args, streams)
        if isinstance(args, K8sEventsArgs) and self.events:
            events = [_recorded_event(e) for e in self.events]
            return {
                "items": [
                    e
                    for e in events
                    if (args.reasons is None or e["reason"] in args.reasons)
                    and (args.involved_object in (None, e["object"]["name"]))
                ][:MAX_OBJECTS],
                "truncated": False,
            }
        return None


def _of_service(labels: dict[str, Any], service: str) -> bool:
    return labels.get("service") == service or str(labels.get("pod", "")).startswith(f"{service}-")


def _replay_logs(
    spec: ToolSpec,
    args: LogsSearchArgs | LogsAggregateArgs | LogsContextArgs,
    streams: list[dict[str, Any]],
) -> dict[str, Any]:
    if isinstance(args, LogsSearchArgs):
        if args.pattern is not None:
            streams = [
                s | {"values": [v for v in s["values"] if args.pattern in v[1]]} for s in streams
            ]
        return compress_logs(streams, args.limit)
    if isinstance(args, LogsAggregateArgs):
        counts: dict[tuple[str, int], int] = {}
        for s in streams:
            group = str(s.get("stream", {}).get(args.group_by, ""))
            for ts, _ in s["values"]:
                bucket = int(ts) // 10**9 // args.step_seconds * args.step_seconds
                counts[(group, bucket)] = counts.get((group, bucket), 0) + 1
        groups = sorted({g for g, _ in counts})
        series = [
            {
                "metric": {args.group_by: g},
                "values": [[b, str(n)] for (gg, b), n in sorted(counts.items()) if gg == g],
            }
            for g in groups
        ]
        return compress_series(series, spec.limits)
    lines = log_lines(streams)
    anchor = int(args.anchor_ts)
    before = [e for e in lines if e[0] <= anchor][-(args.before + 1) :]
    after = [e for e in lines if e[0] > anchor][: args.after]
    return {
        "lines": [
            {"at": str(ts), "pod": pod, "line": line[:MAX_LINE], "anchor": ts == anchor}
            for ts, line, pod in before + after
        ],
        "truncated": False,
    }


def _recorded_event(event: dict[str, Any]) -> dict[str, Any]:
    """Recorded events name their object as `Kind/name`; the live view splits it."""
    kind, _, name = str(event.get("object", "")).partition("/")
    return k8s_view.event(
        {
            "type": event.get("type"),
            "reason": event.get("reason"),
            "message": event.get("message"),
            "involvedObject": {"kind": kind, "name": name},
            "count": event.get("count"),
            "firstTimestamp": event.get("first_at"),
            "lastTimestamp": event.get("last_at"),
        }
    )


def _rows(data: dict[str, Any]) -> int:
    rows = data.get("alerts", data.get("lines", data.get("items", data.get("revisions", []))))
    return rows if isinstance(rows, int) else len(rows)


def _summary(data: dict[str, Any] | None) -> str | None:
    if data is None:
        return None
    if "patterns" in data:
        return f"{data['lines']} log lines, {len(data['patterns'])} patterns"
    if "lines" in data:
        return f"{len(data['lines'])} log lines around the anchor"
    if "items" in data:
        return f"{len(data['items'])} {data.get('kind', 'events')}"
    if "revisions" in data:
        return f"{len(data['revisions'])} revisions"
    if "series" in data:
        flagged = sum(1 for s in data["series"] if s["anomalies"])
        return f"{len(data['series'])} series, {flagged} with anomalies"
    if "alerts" in data:
        return f"{len(data['alerts'])} alerts"
    return None
