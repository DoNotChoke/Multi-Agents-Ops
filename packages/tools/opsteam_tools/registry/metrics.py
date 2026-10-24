"""Metrics tools over Prometheus: templated range and instant queries, and active alerts."""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opsteam_tools.registry.spec import Limits, ToolSpec

TemplateName = Literal[
    "http_latency_p95",
    "http_error_rate",
    "request_rate",
    "pod_restarts",
    "memory_working_set",
    "cpu_saturation",
]
Value = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.\-/:]{1,128}$")]
MAX_POINTS = 11_000
_STRICT = ConfigDict(extra="forbid")


class TemplateParams(BaseModel):
    model_config = _STRICT
    service: Value
    environment: Value


class QueryRangeArgs(BaseModel):
    model_config = _STRICT
    query_template: TemplateName
    params: TemplateParams
    window_seconds: int = Field(ge=60, le=6 * 3600)
    step_seconds: int = Field(ge=15, le=3600)

    @model_validator(mode="after")
    def _bounded_points(self) -> Self:
        if self.window_seconds // self.step_seconds > MAX_POINTS:
            raise ValueError(f"window/step exceeds {MAX_POINTS} points")
        return self


class QueryInstantArgs(BaseModel):
    model_config = _STRICT
    query_template: TemplateName
    params: TemplateParams
    at: datetime | None = None


class AlertsListArgs(BaseModel):
    model_config = _STRICT
    state: Literal["firing", "pending"] | None = None
    service: Value | None = None


_METRICS = frozenset({"metrics"})

QUERY_RANGE = ToolSpec(
    name="metrics.query_range",
    description=(
        "Time series of a named metric template for one catalog service over the last "
        "window_seconds, sampled every step_seconds; returns summary stats and anomalous intervals."
    ),
    args=QueryRangeArgs,
    integration_type="prometheus",
    allowed_agents=_METRICS,
    timeout_ms=15_000,
    limits=Limits(
        max_bytes=1 << 20,
        max_series=20,
        max_window_s=6 * 3600,
        min_step_s=15,
        max_points=MAX_POINTS,
    ),
)

QUERY_INSTANT = ToolSpec(
    name="metrics.query_instant",
    description="Current (or `at`) value of a named metric template for one catalog service.",
    args=QueryInstantArgs,
    integration_type="prometheus",
    allowed_agents=_METRICS,
    timeout_ms=10_000,
    limits=Limits(max_bytes=256 << 10, max_series=200),
)

ALERTS_LIST = ToolSpec(
    name="alerts.list",
    description="Alerts Prometheus currently has firing or pending, optionally for one service.",
    args=AlertsListArgs,
    integration_type="prometheus",
    allowed_agents=_METRICS,
    timeout_ms=10_000,
    limits=Limits(max_bytes=256 << 10, max_series=200),
)
