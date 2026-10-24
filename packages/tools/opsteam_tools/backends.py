"""How a validated tool call reaches the customer's system: directly today, via the Runner later."""

from collections.abc import Mapping, Set
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

import httpx
from opsteam_core.clock import Clock, SystemClock
from pydantic import BaseModel, SecretStr

from opsteam_tools.compress import compress_series
from opsteam_tools.connectors.prometheus import PrometheusConnector
from opsteam_tools.registry.metrics import AlertsListArgs, QueryInstantArgs, QueryRangeArgs
from opsteam_tools.registry.spec import ToolSpec
from opsteam_tools.registry.templates import render


@dataclass(frozen=True)
class IntegrationTarget:
    """What a backend needs about one integration; the Gateway loads it under the tenant's RLS."""

    endpoint: str
    credentials: SecretStr | None
    metric_mapping: Mapping[str, str]


class IntegrationBackend(Protocol):
    """Runs a tool whose arguments already passed the ToolSpec schema."""

    async def run(
        self,
        spec: ToolSpec,
        args: BaseModel,
        target: IntegrationTarget,
        catalog: Set[tuple[str, str]],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class DirectBackend:
    """Calls the integration's Prometheus from the control plane (integrations with mode=direct)."""

    dev_allow: frozenset[str] = frozenset()
    transport: httpx.AsyncBaseTransport | None = None
    clock: Clock = field(default_factory=SystemClock)

    async def run(
        self,
        spec: ToolSpec,
        args: BaseModel,
        target: IntegrationTarget,
        catalog: Set[tuple[str, str]],
    ) -> dict[str, Any]:
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
            return compress_series(data.get("result", []), spec.limits)
        if isinstance(args, AlertsListArgs):
            return _alerts(await prometheus.alerts(), args, spec.limits.max_series)
        raise TypeError(f"no direct handler for {spec.name}")


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
