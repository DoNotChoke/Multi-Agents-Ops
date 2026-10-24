"""Every ToolExecutor behaves the same at the contract level: valid ToolResults, typed refusals."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from opsteam_contracts.tool_request import ToolRequest
from opsteam_contracts.tool_result import ToolResult
from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id
from opsteam_tools.backends import DirectBackend, IntegrationTarget
from opsteam_tools.executor_iface import ToolExecutor
from opsteam_tools.executors import FakeToolExecutor, ReplayToolExecutor, dispatch
from pydantic import SecretStr

TOKEN = "dev-only-contract-token-42"
REPLAY = Path(__file__).parents[2] / "evals/replay/checkout-db-pool-after-release/replay.json"
INTEGRATION = new_id("int")
CTX = RequestContext(
    tenant_id=new_id("tnt"),
    principal=Principal(type="system", id="test"),
    roles=(),
    scopes=frozenset(),
)


def fake_prometheus(request: httpx.Request) -> httpx.Response:
    values = [[1_792_000_000 + 15 * i, "0.2" if i < 10 else "2.4"] for i in range(30)]
    result = [{"metric": {"service": "checkout"}, "values": values}]
    return httpx.Response(
        200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
    )


class DirectExecutor:
    """The dispatch path the Gateway will use, minus its checks, over a fake Prometheus."""

    def __init__(
        self, handler: Callable[[httpx.Request], httpx.Response] = fake_prometheus
    ) -> None:
        self.backend = DirectBackend(transport=httpx.MockTransport(handler))
        self.target = IntegrationTarget(
            endpoint="https://93.184.216.34", credentials=SecretStr(TOKEN), metric_mapping={}
        )

    async def execute(self, ctx: RequestContext, request: ToolRequest, *, agent: str) -> ToolResult:
        catalog = {("checkout", "lab")}
        return await dispatch(request, agent, self.backend, self.target, catalog)


EXECUTORS: dict[str, Callable[[], ToolExecutor]] = {
    "fake": FakeToolExecutor,
    "replay": lambda: ReplayToolExecutor(REPLAY),
    "direct": DirectExecutor,
}


def _request(**change: Any) -> ToolRequest:
    body: dict[str, Any] = {
        "tool": "metrics.query_range",
        "integration_id": INTEGRATION,
        "resource": {"service": "checkout", "environment": "lab"},
        "arguments": {
            "query_template": "http_latency_p95",
            "params": {"service": "checkout", "environment": "lab"},
            "window_seconds": 600,
            "step_seconds": 15,
        },
    }
    return ToolRequest.model_validate(body | change)


@pytest.fixture(params=sorted(EXECUTORS))
def executor(request: pytest.FixtureRequest) -> ToolExecutor:
    return EXECUTORS[request.param]()


async def test_valid_request_yields_a_valid_ok_result(executor: ToolExecutor) -> None:
    result = await executor.execute(CTX, _request(), agent="metrics")
    ToolResult.model_validate(result.model_dump())
    assert result.status == "ok", result.error
    assert result.tool_call_id.startswith("tc_")
    assert result.data is not None and isinstance(result.data.truncated, bool)
    assert TOKEN not in result.model_dump_json()


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"tool": "shell.exec"}, "tool.unknown"),
        ({"arguments": {"promql": "up"}}, "tool.invalid_arguments"),
        ({"resource": {"service": "payment", "environment": "lab"}}, "tool.invalid_arguments"),
    ],
)
async def test_bad_requests_are_denied_with_a_code(
    executor: ToolExecutor, change: dict[str, Any], code: str
) -> None:
    result = await executor.execute(CTX, _request(**change), agent="metrics")
    assert (result.status, result.error and result.error.code) == ("denied", code)
    assert result.data is None and "promql" not in result.model_dump_json()


async def test_agent_outside_the_allowlist_is_denied(executor: ToolExecutor) -> None:
    result = await executor.execute(CTX, _request(), agent="logs")
    assert (result.status, result.error and result.error.code) == ("denied", "tool.not_allowed")


async def test_direct_timeout_and_oversize_map_to_statuses() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def huge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (2 << 20))

    assert (
        await DirectExecutor(slow).execute(CTX, _request(), agent="metrics")
    ).status == "timeout"
    big = await DirectExecutor(huge).execute(CTX, _request(), agent="metrics")
    assert big.status == "limit_exceeded"


async def test_direct_service_outside_the_catalog_is_denied() -> None:
    params = {"service": "orders", "environment": "lab"}
    request = _request(
        resource=params,
        arguments={
            "query_template": "request_rate",
            "params": params,
            "window_seconds": 600,
            "step_seconds": 15,
        },
    )
    result = await DirectExecutor().execute(CTX, request, agent="metrics")
    assert (result.status, result.error and result.error.code) == (
        "denied",
        "tool.invalid_arguments",
    )


async def test_replay_finds_the_recorded_latency_jump() -> None:
    result = await ReplayToolExecutor(REPLAY).execute(CTX, _request(), agent="metrics")
    assert result.data is not None
    (series,) = result.data.model_dump()["series"]
    assert series["labels"] == {"service": "checkout"} and series["anomalies"]


async def test_replay_without_recorded_data_says_so() -> None:
    args = {
        "query_template": "http_error_rate",
        "params": {"service": "checkout", "environment": "lab"},
        "window_seconds": 600,
        "step_seconds": 15,
    }
    result = await ReplayToolExecutor(REPLAY).execute(
        CTX, _request(arguments=args), agent="metrics"
    )
    assert (result.status, result.error and result.error.code) == ("error", "replay.missing")


async def test_fake_records_calls_and_returns_scripted_results() -> None:
    scripted = ToolResult.model_validate(
        {
            "schema_version": 1,
            "tool_call_id": new_id("tc"),
            "status": "timeout",
            "evidence_ids": [],
            "usage": {"duration_ms": 0, "bytes": 0, "rows": 0, "series": 0},
        }
    )
    fake = FakeToolExecutor({"metrics.query_range": scripted})
    assert (await fake.execute(CTX, _request(), agent="metrics")).status == "timeout"
    assert [(r.tool, a) for _, r, a in fake.calls] == [("metrics.query_range", "metrics")]
