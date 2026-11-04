"""How a validated tool call reaches the customer's system: directly today, via the Runner later."""

from collections.abc import Mapping, Set
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from opsteam_core.canonical import JsonValue
from opsteam_core.clock import Clock, SystemClock
from pydantic import BaseModel, SecretStr

from opsteam_tools.compress import MAX_LINE, compress_logs, compress_series, log_lines
from opsteam_tools.connectors.loki import LokiConnector
from opsteam_tools.connectors.prometheus import PrometheusConnector
from opsteam_tools.registry.logs import LogsAggregateArgs, LogsContextArgs, LogsSearchArgs, selector
from opsteam_tools.registry.metrics import AlertsListArgs, QueryInstantArgs, QueryRangeArgs
from opsteam_tools.registry.spec import ToolSpec
from opsteam_tools.registry.templates import render


@dataclass(frozen=True)
class IntegrationTarget:
    """What a backend needs about one integration; the Gateway loads it under the tenant's RLS."""

    endpoint: str
    credentials: SecretStr | None
    metric_mapping: Mapping[str, str]
    ca_cert: str | None = None


@dataclass(frozen=True)
class Fetched:
    """`data` is the compressed answer for the agent; `raw` is what the integration returned."""

    data: dict[str, Any]
    raw: JsonValue


class IntegrationBackend(Protocol):
    """Runs a tool whose arguments already passed the ToolSpec schema."""

    async def run(
        self,
        spec: ToolSpec,
        args: BaseModel,
        target: IntegrationTarget,
        catalog: Set[tuple[str, str]],
    ) -> Fetched: ...


@dataclass(frozen=True)
class DirectBackend:
    """Calls the integration's system from the control plane (integrations with mode=direct)."""

    dev_allow: frozenset[str] = frozenset()
    transport: httpx.AsyncBaseTransport | None = None
    clock: Clock = field(default_factory=SystemClock)

    async def run(
        self,
        spec: ToolSpec,
        args: BaseModel,
        target: IntegrationTarget,
        catalog: Set[tuple[str, str]],
    ) -> Fetched:
        if spec.integration_type == "loki":
            return await self._logs(spec, args, target, catalog)
        prometheus = PrometheusConnector(
            target.endpoint,
            target.credentials,
            dev_allow=self.dev_allow,
            timeout_s=spec.timeout_ms / 1000,
            max_bytes=spec.limits.max_bytes,
            transport=self.transport,
        )
        if isinstance(args, QueryRangeArgs | QueryInstantArgs):
            params = args.params.model_dump()
            promql = render(
                args.query_template, params, mapping=target.metric_mapping, catalog=catalog
            )
            if isinstance(args, QueryRangeArgs):
                end = self.clock.now()
                start = end - timedelta(seconds=args.window_seconds)
                data = await prometheus.query_range(promql, start, end, args.step_seconds)
            else:
                data = await prometheus.query(promql, args.at)
            return Fetched(compress_series(data.get("result", []), spec.limits), data)
        if isinstance(args, AlertsListArgs):
            alerts = await prometheus.alerts()
            return Fetched(_alerts(alerts, args, spec.limits.max_series), {"alerts": alerts})
        raise TypeError(f"no direct handler for {spec.name}")

    async def _logs(
        self,
        spec: ToolSpec,
        args: BaseModel,
        target: IntegrationTarget,
        catalog: Set[tuple[str, str]],
    ) -> Fetched:
        loki = LokiConnector(
            target.endpoint,
            target.credentials,
            dev_allow=self.dev_allow,
            timeout_s=spec.timeout_ms / 1000,
            max_bytes=spec.limits.max_bytes,
            transport=self.transport,
        )
        end = int(self.clock.now().timestamp() * 1e9)
        if isinstance(args, LogsSearchArgs):
            start = end - args.window_seconds * 10**9
            data = await loki.query_range(selector(args, catalog), start, end, limit=args.limit)
            return Fetched(compress_logs(data["result"], args.limit), data)
        if isinstance(args, LogsAggregateArgs):
            start = end - args.window_seconds * 10**9
            step, lines = args.step_seconds, selector(args, catalog)
            logql = f"sum by ({args.group_by}) (count_over_time({lines} [{step}s]))"
            data = await loki.query_range(logql, start, end, step_s=step)
            return Fetched(compress_series(data["result"], spec.limits), data)
        if isinstance(args, LogsContextArgs):
            return await _context(loki, args, selector(args, catalog))
        raise TypeError(f"no direct handler for {spec.name}")


async def _context(loki: LokiConnector, args: LogsContextArgs, logql: str) -> Fetched:
    """Lines around the anchor: Loki's end is exclusive, so +1 keeps the anchor in `before`."""
    anchor, hour = int(args.anchor_ts), 3600 * 10**9
    before = await loki.query_range(logql, anchor - hour, anchor + 1, limit=args.before + 1)
    after: dict[str, Any] = {"result": []}
    if args.after:
        after = await loki.query_range(
            logql, anchor + 1, anchor + hour, limit=args.after, direction="forward"
        )
    lines = [
        {"at": _iso_ns(ts), "pod": pod, "line": line[:MAX_LINE], "anchor": ts == anchor}
        for ts, line, pod in log_lines(before["result"]) + log_lines(after["result"])
    ]
    return Fetched({"lines": lines, "truncated": False}, {"before": before, "after": after})


def _iso_ns(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1e9, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _alerts(alerts: list[dict[str, Any]], args: AlertsListArgs, limit: int) -> dict[str, Any]:
    wanted = [
        {
            "labels": a.get("labels", {}),
            "state": a.get("state"),
            "active_at": a.get("activeAt"),
        }
        for a in alerts
        if (args.state is None or a.get("state") == args.state)
        and (args.service is None or a.get("labels", {}).get("service") == args.service)
    ]
    return {"alerts": wanted[:limit], "truncated": len(wanted) > limit}
