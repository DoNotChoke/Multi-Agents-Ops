from collections.abc import Callable
from typing import Any

import httpx
import pytest
from opsteam_core.errors import NetguardBlocked
from opsteam_tools.connectors.alertmanager import AlertmanagerConnector
from opsteam_tools.connectors.prometheus import ConnectorError, ResponseTooLarge

PUBLIC_IP = "93.184.216.34"
ALERT = {
    "labels": {"alertname": "HighErrorRate", "service": "checkout"},
    "annotations": {"summary": "checkout returns 5xx"},
    "startsAt": "2026-10-29T09:00:00Z",
    "endsAt": "2026-10-29T09:10:00Z",
    "generatorURL": "http://prometheus:9090/graph",
    "status": {"state": "active", "silencedBy": [], "inhibitedBy": []},
}


class Resolver:
    def __init__(self, *ips: str) -> None:
        self.ips = list(ips or [PUBLIC_IP])

    async def resolve(self, host: str, port: int) -> list[str]:
        return self.ips


def _connector(
    handler: Callable[[httpx.Request], httpx.Response],
    endpoint: str = "https://am.example.com:9093",
    *ips: str,
    **kw: Any,
) -> AlertmanagerConnector:
    return AlertmanagerConnector(
        endpoint, transport=httpx.MockTransport(handler), resolver=Resolver(*ips), **kw
    )


async def test_reads_only_alerts_that_would_be_sent_from_the_vetted_ip() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[ALERT])

    assert await _connector(handler).alerts() == [ALERT]
    (request,) = seen
    assert (request.url.host, request.url.port, request.url.path) == (
        PUBLIC_IP,
        9093,
        "/api/v2/alerts",
    )
    assert dict(request.url.params) == {
        "active": "true",
        "silenced": "false",
        "inhibited": "false",
    }
    assert request.headers["host"] == "am.example.com:9093"


async def test_a_path_prefix_is_kept() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    await _connector(handler, "https://am.example.com/alertmanager").alerts()
    assert seen[0].url.path == "/alertmanager/api/v2/alerts"


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(200, json={"alerts": []}), ConnectorError),
        (httpx.Response(200, text="not json"), ConnectorError),
        (httpx.Response(500, text="secret stack"), ConnectorError),
        (httpx.Response(302, headers={"location": "http://10.0.0.1/"}), ConnectorError),
        (httpx.Response(200, content=b"[" + b" " * 2048 + b"]"), ResponseTooLarge),
    ],
)
async def test_bad_answers_fail_without_echoing_them(
    response: httpx.Response, error: type[Exception]
) -> None:
    with pytest.raises(error) as exc:
        await _connector(lambda _: response, max_bytes=1024).alerts()
    assert "secret stack" not in str(exc.value)
    assert "alertmanager" in str(exc.value)


async def test_internal_addresses_are_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may leave")

    with pytest.raises(NetguardBlocked):
        await _connector(handler, "https://am.example.com:9093", "10.0.0.7").alerts()
