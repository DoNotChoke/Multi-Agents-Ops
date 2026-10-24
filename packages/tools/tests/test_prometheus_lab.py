"""Against the kind lab's Prometheus; run with `uv run pytest -m lab` after `make lab-up`."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from opsteam_contracts.tool_request import ToolRequest
from opsteam_core.ids import new_id
from opsteam_tools.backends import DirectBackend, IntegrationTarget
from opsteam_tools.connectors.prometheus import PrometheusConnector
from opsteam_tools.executors import dispatch
from opsteam_tools.registry.templates import TEMPLATES, render

pytestmark = pytest.mark.lab

LAB = PrometheusConnector("http://127.0.0.1:30090", dev_allow=frozenset({"127.0.0.1:30090"}))


async def test_build_info_and_up() -> None:
    assert (await LAB.build_info())["version"]
    assert (await LAB.query("up"))["result"]


async def test_query_range_over_the_last_ten_minutes() -> None:
    end = datetime.now(UTC)
    data = await LAB.query_range("sum(up)", end - timedelta(minutes=10), end, step_s=60)
    assert data["resultType"] == "matrix"
    assert len(data["result"][0]["values"]) >= 5


async def test_alerts_is_a_list() -> None:
    assert isinstance(await LAB.alerts(), list)


LAB_MAPPING = json.loads(
    (Path(__file__).parents[3] / "deploy" / "lab" / "metric_mapping.json").read_text()
)
# Error rate is legitimately empty while the shop serves no 5xx.
MAY_BE_EMPTY = {"http_error_rate"}


@pytest.mark.parametrize("name", sorted(TEMPLATES))
async def test_lab_mapping_queries_return_data(name: str) -> None:
    params = {"service": "checkout", "environment": "lab"}
    promql = render(name, params, mapping=LAB_MAPPING, catalog={("checkout", "lab")})
    result = (await LAB.query(promql))["result"]
    assert result or name in MAY_BE_EMPTY, promql


async def test_direct_dispatch_against_the_lab() -> None:
    request = ToolRequest.model_validate(
        {
            "tool": "metrics.query_range",
            "integration_id": new_id("int"),
            "resource": {"service": "checkout", "environment": "lab"},
            "arguments": {
                "query_template": "http_latency_p95",
                "params": {"service": "checkout", "environment": "lab"},
                "window_seconds": 900,
                "step_seconds": 30,
            },
        }
    )
    backend = DirectBackend(dev_allow=frozenset({"127.0.0.1:30090"}))
    target = IntegrationTarget("http://127.0.0.1:30090", None, LAB_MAPPING)
    result = await dispatch(request, "metrics", backend, target, {("checkout", "lab")})
    assert result.status == "ok", result.error
    assert result.data is not None and result.data.model_dump()["series"][0]["stats"]["points"] > 0
