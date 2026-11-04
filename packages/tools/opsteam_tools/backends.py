"""How a validated tool call reaches the customer's system: directly today, via the Runner later."""

from collections.abc import Mapping, Set
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from opsteam_core.canonical import JsonValue
from opsteam_core.clock import Clock, SystemClock
from pydantic import BaseModel, SecretStr

from opsteam_tools import k8s_view
from opsteam_tools.compress import MAX_LINE, compress_logs, compress_series, log_lines
from opsteam_tools.connectors.http import ConnectorError
from opsteam_tools.connectors.kubernetes import KubernetesConnector
from opsteam_tools.connectors.loki import LokiConnector
from opsteam_tools.connectors.prometheus import PrometheusConnector
from opsteam_tools.registry.k8s import (
    MAX_OBJECTS,
    MAX_REVISIONS,
    PATHS,
    K8sEventsArgs,
    K8sResourcesArgs,
    K8sRolloutsArgs,
)
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
        if spec.integration_type == "kubernetes":
            return await self._k8s(spec, args, target)
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

    async def _k8s(self, spec: ToolSpec, args: BaseModel, target: IntegrationTarget) -> Fetched:
        """Only allowlisted views leave this method; they are also all that evidence keeps."""
        if target.credentials is None or not target.ca_cert:
            raise ConnectorError("kubernetes integration has no token or CA")
        k8s = KubernetesConnector(
            target.endpoint,
            target.credentials,
            target.ca_cert,
            dev_allow=self.dev_allow,
            timeout_s=spec.timeout_ms / 1000,
            max_bytes=spec.limits.max_bytes,
            transport=self.transport,
        )
        if isinstance(args, K8sResourcesArgs):
            params = {"limit": str(MAX_OBJECTS)}
            if args.name is not None:
                params["fieldSelector"] = f"metadata.name={args.name}"
            if args.label_selector is not None:
                params["labelSelector"] = args.label_selector
            found, more = await k8s.items(PATHS[args.kind].format(namespace=args.namespace), params)
            items = [k8s_view.VIEWS[args.kind](o) for o in found]
            return _fetched({"kind": args.kind, "items": items, "truncated": more})
        if isinstance(args, K8sEventsArgs):
            return _fetched(await self._events(k8s, args))
        if isinstance(args, K8sRolloutsArgs):
            return _fetched(await _rollouts(k8s, args))
        raise TypeError(f"no direct handler for {spec.name}")

    async def _events(self, k8s: KubernetesConnector, args: K8sEventsArgs) -> dict[str, Any]:
        params = {}
        if args.involved_object is not None:
            params["fieldSelector"] = f"involvedObject.name={args.involved_object}"
        # shortcut: one unpaged list bounded by max_bytes; page with `continue` if a namespace
        # ever keeps thousands of live events.
        found, more = await k8s.items(PATHS["events"].format(namespace=args.namespace), params)
        since = self.clock.now() - timedelta(seconds=args.window_seconds)
        events = [
            e
            for e in map(k8s_view.event, found)
            if (args.reasons is None or e["reason"] in args.reasons)
            and e["last_at"] is not None
            and _parse_time(e["last_at"]) >= since
        ]
        events.sort(key=lambda e: _parse_time(e["last_at"]), reverse=True)
        return {"items": events[:MAX_OBJECTS], "truncated": more or len(events) > MAX_OBJECTS}


def _fetched(view: dict[str, Any]) -> Fetched:
    return Fetched(view, view)


async def _rollouts(k8s: KubernetesConnector, args: K8sRolloutsArgs) -> dict[str, Any]:
    """The deployment and the replicasets it owns, newest revision first."""
    path = PATHS["deployments"].format(namespace=args.namespace)
    found, _ = await k8s.items(path, {"fieldSelector": f"metadata.name={args.name}"})
    if not found:
        return {"deployment": None, "revisions": [], "truncated": False}
    match = found[0].get("spec", {}).get("selector", {}).get("matchLabels")
    labels = ",".join(f"{k}={v}" for k, v in match.items()) if isinstance(match, dict) else ""
    path = PATHS["replicasets"].format(namespace=args.namespace)
    sets, _ = await k8s.items(path, {"labelSelector": labels} if labels else {})
    owned = [r for r in map(k8s_view.replicaset, sets) if r["deployment"] == args.name]
    owned.sort(key=lambda r: int(r["revision"]) if (r["revision"] or "").isdigit() else 0)
    owned.reverse()
    return {
        "deployment": k8s_view.deployment(found[0]),
        "revisions": owned[:MAX_REVISIONS],
        "truncated": len(owned) > MAX_REVISIONS,
    }


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
