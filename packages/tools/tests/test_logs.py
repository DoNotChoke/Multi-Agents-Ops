import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from opsteam_contracts.tool_request import ToolRequest
from opsteam_tools.backends import DirectBackend, IntegrationTarget
from opsteam_tools.compress import compress_logs, log_pattern
from opsteam_tools.executors import Denied, check_request
from opsteam_tools.registry import TOOLS
from opsteam_tools.registry.logs import (
    LogsAggregateArgs,
    LogsContextArgs,
    LogsSearchArgs,
    selector,
)
from opsteam_tools.registry.templates import TemplateError
from pydantic import ValidationError

CATALOG = {("checkout", "shop")}
PARAMS = {"service": "checkout", "environment": "shop"}
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
NOW_NS = int(NOW.timestamp()) * 10**9
LAB_LINE = (
    '{"time":"2026-11-04T17:47:34.440805583Z","level":"ERROR","msg":"checkout call failed",'
    '"route":"/checkout","status":502,"duration_ms":0,"trace_id":"ccf694bea503807b109e01f88501cb8d"}'
)


class Clock:
    def now(self) -> datetime:
        return NOW


def _search(**extra: Any) -> LogsSearchArgs:
    return LogsSearchArgs.model_validate(
        {"selector_template": "app_logs", "params": PARAMS, "window_seconds": 600} | extra
    )


def _streams(*entries: tuple[int, str], pod: str = "checkout-1") -> list[dict[str, Any]]:
    return [{"stream": {"pod": pod}, "values": [[str(ts), line] for ts, line in entries]}]


def test_selectors_render_from_the_catalog_only() -> None:
    assert selector(_search(), CATALOG) == '{namespace="shop", service="checkout"}'
    assert selector(_search(selector_template="error_logs"), CATALOG) == (
        '{namespace="shop", service="checkout"} |~ "(?i)(error|exception|fatal|panic)"'
    )
    assert selector(_search(pattern='pool "exhausted"'), CATALOG) == (
        '{namespace="shop", service="checkout"} |= `pool "exhausted"`'
    )
    with pytest.raises(TemplateError, match="catalog"):
        selector(_search(params={"service": "payment", "environment": "shop"}), CATALOG)


@pytest.mark.parametrize(
    "extra",
    [
        {"pattern": 'x` or {job=~".+"} `'},  # would close the raw string
        {"pattern": "line\nbreak"},
        {"pattern": ""},
        {"pattern": "x" * 129},
        {"selector_template": "raw_logql"},
        {"window_seconds": 3 * 3600 + 1},
        {"limit": 201},
        {"params": {"service": 'x"}', "environment": "shop"}},
    ],
)
def test_search_arguments_are_bounded(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _search(**extra)


def test_log_pattern_folds_variable_parts() -> None:
    assert log_pattern(LAB_LINE) == (
        '{"time":"<num>-<num>-<num>T<num>:<num>:<num>Z","level":"ERROR","msg":"checkout call '
        'failed","route":"/checkout","status":<num>,"duration_ms":<num>,"trace_id":"<hex>"}'
    )
    assert log_pattern("from 10.0.0.12:5432 id 3f2b8c1e-0d4a-4c6e-9a7b-1e2d3c4b5a69 x") == (
        "from <ip> id <uuid> x"
    )
    assert log_pattern("deadbeef cafe") == "deadbeef cafe"  # words made of hex letters stay


def test_compress_logs_counts_patterns_and_keeps_few_short_exemplars() -> None:
    long = "boom " + "x" * 900
    entries = [(NOW_NS + i, f"request {i} took {i * 3} ms") for i in range(30)]
    entries += [(NOW_NS + 100 + i, f"unique failure kind {chr(65 + i)} {long}") for i in range(25)]
    out = compress_logs(_streams(*entries), limit=200)
    assert out["lines"] == 55 and out["truncated"] is False
    top = out["patterns"][0]
    assert (top["pattern"], top["count"]) == ("request <num> took <num> ms", 30)
    assert top["first_at"] == "2026-11-05T12:00:00Z"
    assert len(out["patterns"]) == 20 and out["dropped_patterns"] == 6
    assert len(out["exemplars"]) == 5
    assert out["exemplars"][0]["anchor_ts"] == str(NOW_NS)
    assert all(len(e["line"]) <= 500 for e in out["exemplars"])
    assert all(len(p["pattern"]) <= 500 for p in out["patterns"])
    assert compress_logs(_streams(*entries[:10]), limit=10)["truncated"] is True
    assert compress_logs([], limit=10)["lines"] == 0


def _backend(seen: list[httpx.Request], answers: list[dict[str, Any]]) -> DirectBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "success", "data": answers.pop(0)})

    return DirectBackend(
        dev_allow=frozenset({"127.0.0.1:30100"}),
        transport=httpx.MockTransport(handler),
        clock=Clock(),
    )


TARGET = IntegrationTarget("http://127.0.0.1:30100", None, {})


async def test_search_queries_the_window_with_the_limit() -> None:
    seen: list[httpx.Request] = []
    streams = _streams((NOW_NS - 5, LAB_LINE))
    backend = _backend(seen, [{"resultType": "streams", "result": streams}])
    args = _search(pattern="call failed", limit=50)
    fetched = await backend.run(TOOLS["logs.search"], args, TARGET, CATALOG)
    (request,) = seen
    assert request.url.path == "/loki/api/v1/query_range"
    assert dict(request.url.params) == {
        "query": '{namespace="shop", service="checkout"} |= `call failed`',
        "start": str(NOW_NS - 600 * 10**9),
        "end": str(NOW_NS),
        "direction": "backward",
        "limit": "50",
    }
    assert fetched.data["lines"] == 1 and fetched.data["exemplars"][0]["pod"] == "checkout-1"
    assert fetched.raw == {"resultType": "streams", "result": streams}


async def test_aggregate_counts_per_group_as_series() -> None:
    seen: list[httpx.Request] = []
    matrix = [{"metric": {"pod": "checkout-1"}, "values": [[1, "10"], [2, "12"], [3, "11"]]}]
    backend = _backend(seen, [{"resultType": "matrix", "result": matrix}])
    args = LogsAggregateArgs.model_validate(
        {
            "selector_template": "error_logs",
            "params": PARAMS,
            "group_by": "pod",
            "window_seconds": 3600,
            "step_seconds": 60,
        }
    )
    fetched = await backend.run(TOOLS["logs.aggregate"], args, TARGET, CATALOG)
    assert seen[0].url.params["query"] == (
        'sum by (pod) (count_over_time({namespace="shop", service="checkout"}'
        ' |~ "(?i)(error|exception|fatal|panic)" [60s]))'
    )
    assert seen[0].url.params["step"] == "60"
    assert fetched.data["series"][0]["labels"] == {"pod": "checkout-1"}


async def test_context_reads_both_sides_of_the_anchor() -> None:
    seen: list[httpx.Request] = []
    anchor = NOW_NS - 10
    before = _streams((anchor - 2, "a"), (anchor, "anchor line"))
    after = _streams((anchor + 3, "b"))
    backend = _backend(seen, [{"result": before}, {"result": after}])
    args = LogsContextArgs.model_validate(
        {"selector_template": "app_logs", "params": PARAMS, "anchor_ts": str(anchor), "after": 5}
    )
    fetched = await backend.run(TOOLS["logs.get_context"], args, TARGET, CATALOG)
    assert [line["line"] for line in fetched.data["lines"]] == ["a", "anchor line", "b"]
    assert [line["anchor"] for line in fetched.data["lines"]] == [False, True, False]
    first, second = (dict(r.url.params) for r in seen)
    assert (first["end"], first["limit"], first["direction"]) == (str(anchor + 1), "21", "backward")
    assert (second["start"], second["limit"], second["direction"]) == (
        str(anchor + 1),
        "5",
        "forward",
    )


def _request(tool: str, arguments: dict[str, Any]) -> ToolRequest:
    return ToolRequest.model_validate(
        {
            "tool": tool,
            "integration_id": "int_01JDEVSEED0000000000000000",
            "resource": {"service": "checkout", "environment": "shop"},
            "arguments": arguments,
        }
    )


def test_only_the_logs_agent_may_call_logs_tools() -> None:
    arguments = {"selector_template": "app_logs", "params": PARAMS, "window_seconds": 600}
    for agent in ("metrics", "infrastructure", "commander"):
        with pytest.raises(Denied) as caught:
            check_request(_request("logs.search", arguments), agent)
        assert caught.value.reason == "tool.not_allowed"
    spec, _ = check_request(_request("logs.search", arguments), "logs")
    assert spec.integration_type == "loki"
    other = arguments | {"params": {"service": "payment", "environment": "shop"}}
    with pytest.raises(Denied, match="requested resource"):
        check_request(_request("logs.search", other), "logs")
    assert json.dumps(sorted(TOOLS)).count("logs.") == 3
