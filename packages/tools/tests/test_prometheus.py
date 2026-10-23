from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from opsteam_core.errors import NetguardBlocked
from opsteam_tools.connectors.prometheus import (
    ConnectorError,
    ConnectorTimeout,
    PrometheusConnector,
    ResponseTooLarge,
)
from pydantic import SecretStr

TOKEN = "dev-only-prom-token-123"
PUBLIC_IP = "93.184.216.34"


class Resolver:
    """Answers each lookup with the next list in `answers`, counting calls."""

    def __init__(self, *answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def resolve(self, host: str, port: int) -> list[str]:
        self.calls += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def _ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})


def _connector(
    handler: Callable[[httpx.Request], httpx.Response],
    endpoint: str = "https://prom.example.com",
    resolver: Resolver | None = None,
    **kw: Any,
) -> PrometheusConnector:
    return PrometheusConnector(
        endpoint,
        SecretStr(TOKEN),
        transport=httpx.MockTransport(handler),
        resolver=resolver or Resolver([PUBLIC_IP]),
        **kw,
    )


async def test_connects_to_the_vetted_ip_with_original_host_and_sni() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok({"resultType": "vector", "result": []})

    await _connector(handler).query("up")
    (request,) = seen
    assert request.url.host == PUBLIC_IP
    assert request.url.path == "/api/v1/query"
    assert request.headers["host"] == "prom.example.com"
    assert request.extensions["sni_hostname"] == "prom.example.com"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)


async def test_dns_answer_change_after_the_check_is_not_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return _ok({"resultType": "vector", "result": []})

    resolver = Resolver([PUBLIC_IP], ["169.254.169.254"])
    await _connector(handler, resolver=resolver).query("up")
    assert seen == [PUBLIC_IP]
    assert resolver.calls == 1


async def test_private_address_is_refused_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent")

    with pytest.raises(NetguardBlocked):
        await _connector(handler, resolver=Resolver(["10.0.0.7"])).query("up")


async def test_redirect_is_not_followed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    with pytest.raises(ConnectorError, match="redirect"):
        await _connector(handler).query("up")
    assert calls == 1


async def test_oversized_response_is_cut_off() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    with pytest.raises(ResponseTooLarge):
        await _connector(handler, max_bytes=1024).query("up")


async def test_timeout_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ConnectorTimeout):
        await _connector(handler).query("up")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text=f"upstream said: Bearer {TOKEN}"),
        httpx.Response(200, json={"status": "error", "error": f"bad token {TOKEN}"}),
        httpx.Response(200, text="not json"),
    ],
)
async def test_errors_never_echo_the_body(response: httpx.Response) -> None:
    with pytest.raises(ConnectorError) as exc:
        await _connector(lambda _: response).query("up")
    assert TOKEN not in str(exc.value)


async def test_query_range_parameters() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok({"resultType": "matrix", "result": []})

    start = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)
    end = datetime(2026, 10, 23, 11, 0, tzinfo=UTC)
    data = await _connector(handler).query_range("rate(x[1m])", start, end, step_s=30)
    assert data == {"resultType": "matrix", "result": []}
    params = dict(seen[0].url.params)
    assert seen[0].url.path == "/api/v1/query_range"
    assert params == {
        "query": "rate(x[1m])",
        "start": str(start.timestamp()),
        "end": str(end.timestamp()),
        "step": "30",
    }


async def test_endpoint_path_prefix_is_kept() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return _ok({"version": "3.15.1"})

    await _connector(handler, endpoint="https://prom.example.com/prometheus/").build_info()
    assert seen == ["/prometheus/api/v1/status/buildinfo"]


async def test_dev_allowed_lab_endpoint_skips_sni() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok({"alerts": []})

    connector = _connector(
        handler, endpoint="http://127.0.0.1:30090", dev_allow=frozenset({"127.0.0.1:30090"})
    )
    assert await connector.alerts() == []
    assert seen[0].url.host == "127.0.0.1" and seen[0].url.port == 30090
    assert "sni_hostname" not in seen[0].extensions
