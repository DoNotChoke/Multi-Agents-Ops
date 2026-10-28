import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from opsteam_core.clock import FakeClock
from opsteam_core.errors import NetguardBlocked
from opsteam_notify.webhook import Channel, DeliveryFailed, Message, WebhookNotifier, signature
from pydantic import SecretStr

PUBLIC_IP = "93.184.216.34"
SECRET = SecretStr("dev-only-signing-secret")
NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
MESSAGE = Message(
    kind="incident.opened",
    incident_id="inc_01A",
    title="checkout errors, token=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    severity="high",
    service="checkout",
    environment="shop",
    started_at=NOW,
    url="http://localhost:3000/incidents/inc_01A",
)


class Resolver:
    def __init__(self, *ips: str) -> None:
        self.ips = list(ips)

    async def resolve(self, host: str, port: int) -> list[str]:
        return self.ips


def _notifier(handler: Callable[[httpx.Request], httpx.Response], *ips: str) -> WebhookNotifier:
    return WebhookNotifier(
        FakeClock(NOW),
        transport=httpx.MockTransport(handler),
        resolver=Resolver(*(ips or (PUBLIC_IP,))),
    )


async def test_posts_signed_json_to_the_vetted_ip() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    channel = Channel("int_01W", "https://hooks.example.com/opsteam?x=1", SECRET)
    await _notifier(handler).send(channel, MESSAGE)
    (request,) = seen
    assert (request.url.host, request.url.path, request.url.query) == (
        PUBLIC_IP,
        "/opsteam",
        b"x=1",
    )
    assert request.headers["host"] == "hooks.example.com"
    assert request.extensions["sni_hostname"] == "hooks.example.com"
    timestamp = request.headers["x-opsteam-timestamp"]
    assert timestamp == str(int(NOW.timestamp()))
    assert request.headers["x-opsteam-signature"] == signature(SECRET, timestamp, request.content)
    incident = json.loads(request.content)["incident"]
    assert incident["severity"] == "high" and incident["url"].endswith("/incidents/inc_01A")


async def test_the_payload_is_redacted_and_carries_no_secret() -> None:
    body = MESSAGE.body().decode()
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in body
    assert "[REDACTED:" in body
    assert SECRET.get_secret_value() not in body


async def test_unsigned_without_a_secret() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    await _notifier(handler).send(Channel("int_01W", "https://hooks.example.com"), MESSAGE)
    assert "x-opsteam-signature" not in seen[0].headers


@pytest.mark.parametrize(
    ("url", "ips"),
    [
        ("https://hooks.example.com", ("10.0.0.5",)),
        ("https://hooks.example.com", ("169.254.169.254",)),
        ("https://127.0.0.1/hook", ()),
        ("http://hooks.example.com", (PUBLIC_IP,)),
        ("file:///etc/passwd", ()),
    ],
)
async def test_internal_targets_are_blocked_before_any_request(url: str, ips: tuple[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may leave")

    with pytest.raises(NetguardBlocked):
        await _notifier(handler, *ips).send(Channel("int_01W", url), MESSAGE)


@pytest.mark.parametrize("status", [301, 404, 500])
async def test_non_2xx_and_redirects_fail(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"location": "http://10.0.0.1/"}, text="secret body")

    with pytest.raises(DeliveryFailed) as exc:
        await _notifier(handler).send(Channel("int_01W", "https://hooks.example.com"), MESSAGE)
    assert "secret body" not in str(exc.value)


async def test_unreachable_and_slow_fail() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    for handler in (refuse, slow):
        with pytest.raises(DeliveryFailed):
            await _notifier(handler).send(Channel("int_01W", "https://hooks.example.com"), MESSAGE)
