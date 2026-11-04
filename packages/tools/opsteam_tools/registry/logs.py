"""Logs tools over Loki: templated selectors searched, counted, or read around one line."""

from collections.abc import Mapping, Set
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from opsteam_tools.registry.metrics import TemplateParams
from opsteam_tools.registry.spec import Limits, ToolSpec
from opsteam_tools.registry.templates import render

SelectorName = Literal["app_logs", "error_logs"]
# Lab and Kubernetes log shippers label streams with the namespace and the workload's service.
SELECTORS: Mapping[str, str] = {
    "app_logs": '{namespace="${environment}", service="${service}"}',
    "error_logs": (
        '{namespace="${environment}", service="${service}"} |~ "(?i)(error|exception|fatal|panic)"'
    ),
}
# A raw LogQL string has no escapes, so only a backtick or a control character could break out.
Pattern = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[^`\x00-\x1f\x7f]+$")]
GroupBy = Literal["pod", "version", "detected_level"]
_STRICT = ConfigDict(extra="forbid")


class LogsSearchArgs(BaseModel):
    model_config = _STRICT
    selector_template: SelectorName
    params: TemplateParams
    pattern: Pattern | None = None
    window_seconds: int = Field(ge=60, le=3 * 3600)
    limit: int = Field(default=100, ge=1, le=200)


class LogsAggregateArgs(BaseModel):
    model_config = _STRICT
    selector_template: SelectorName
    params: TemplateParams
    group_by: GroupBy
    window_seconds: int = Field(ge=60, le=6 * 3600)
    step_seconds: int = Field(ge=60, le=3600)


class LogsContextArgs(BaseModel):
    """`anchor_ts` is an exemplar's nanosecond timestamp from an earlier logs.search."""

    model_config = _STRICT
    selector_template: SelectorName
    params: TemplateParams
    anchor_ts: str = Field(pattern=r"^[0-9]{1,20}$")
    before: int = Field(default=20, ge=0, le=100)
    after: int = Field(default=20, ge=0, le=100)


def selector(
    args: LogsSearchArgs | LogsAggregateArgs | LogsContextArgs, catalog: Set[tuple[str, str]]
) -> str:
    """The LogQL selector for `args`, with its plain-text line filter when it has one."""
    logql = render(
        args.selector_template,
        args.params.model_dump(),
        mapping={},
        catalog=catalog,
        templates=SELECTORS,
    )
    if isinstance(args, LogsSearchArgs) and args.pattern is not None:
        logql += f" |= `{args.pattern}`"
    return logql


_LOGS = frozenset({"logs"})

LOGS_SEARCH = ToolSpec(
    name="logs.search",
    description=(
        "Log lines of one catalog service over the last window_seconds, optionally containing a "
        "plain-text pattern; returns pattern counts, first/last seen, and up to 5 exemplars."
    ),
    args=LogsSearchArgs,
    integration_type="loki",
    allowed_agents=_LOGS,
    timeout_ms=15_000,
    limits=Limits(max_bytes=512 << 10, max_series=200, max_window_s=3 * 3600),
)

LOGS_AGGREGATE = ToolSpec(
    name="logs.aggregate",
    description=(
        "Count of one catalog service's log lines per step_seconds, grouped by pod, version or "
        "detected level; returns summary stats and anomalous intervals per group."
    ),
    args=LogsAggregateArgs,
    integration_type="loki",
    allowed_agents=_LOGS,
    timeout_ms=15_000,
    limits=Limits(
        max_bytes=1 << 20, max_series=50, max_window_s=6 * 3600, min_step_s=60, max_points=360
    ),
)

LOGS_GET_CONTEXT = ToolSpec(
    name="logs.get_context",
    description="Raw log lines just before and after an exemplar's anchor_ts, for one service.",
    args=LogsContextArgs,
    integration_type="loki",
    allowed_agents=_LOGS,
    timeout_ms=10_000,
    limits=Limits(max_bytes=256 << 10, max_series=0),
)
