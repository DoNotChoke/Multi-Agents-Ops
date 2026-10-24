from typing import Any, get_args

import pytest
from opsteam_tools.registry import TOOLS
from opsteam_tools.registry.metrics import QueryRangeArgs, TemplateName
from opsteam_tools.registry.templates import TEMPLATES
from pydantic import ValidationError

PARAMS = {"service": "checkout", "environment": "prod"}


def test_metrics_tools_are_registered_read_only_for_the_metrics_agent() -> None:
    assert {"metrics.query_range", "metrics.query_instant", "alerts.list"} <= set(TOOLS)
    for spec in TOOLS.values():
        assert spec.permission == "READ"
        assert spec.allowed_agents == frozenset({"metrics"})
        assert spec.integration_type == "prometheus"


def test_template_enum_matches_the_templates() -> None:
    assert set(get_args(TemplateName)) == set(TEMPLATES)


def test_valid_range_args() -> None:
    args = QueryRangeArgs.model_validate(
        {
            "query_template": "http_latency_p95",
            "params": PARAMS,
            "window_seconds": 3600,
            "step_seconds": 30,
        }
    )
    assert args.window_seconds // args.step_seconds == 120


@pytest.mark.parametrize(
    "change",
    [
        {"window_seconds": 7 * 3600},
        {"step_seconds": 5},
        {"window_seconds": 21600, "step_seconds": 1},
        {"query_template": "raw_promql"},
        {"promql": "up"},
        {"tenant_id": "tnt_x"},
        {"params": PARAMS | {"service": 'x"}'}},
        {"params": PARAMS | {"extra": "y"}},
    ],
)
def test_invalid_range_args(change: dict[str, Any]) -> None:
    body = {
        "query_template": "http_latency_p95",
        "params": PARAMS,
        "window_seconds": 3600,
        "step_seconds": 30,
    }
    with pytest.raises(ValidationError):
        QueryRangeArgs.model_validate(body | change)


def test_specs_describe_themselves_for_the_llm() -> None:
    spec = TOOLS["metrics.query_range"]
    schema = spec.args.model_json_schema()
    assert schema["additionalProperties"] is False
    assert (
        spec.description and spec.limits.max_window_s == 21600 and spec.limits.max_points == 11000
    )
