"""Incident notifications: the message, the Notifier interface, and a signed generic webhook."""

import hashlib
import hmac
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from opsteam_core.canonical import canonical_json
from opsteam_core.clock import Clock
from opsteam_core.errors import OpsteamError
from opsteam_core.netguard import Resolver, check_url, host_header
from opsteam_core.redact import redact
from pydantic import SecretStr


class DeliveryFailed(OpsteamError):
    """A channel did not accept the message; the text never includes the response body."""

    code = "notify.delivery_failed"


@dataclass(frozen=True)
class Message:
    """What a notification says: redacted fields and a link, never raw logs."""

    kind: str
    incident_id: str
    title: str
    severity: str
    service: str | None
    environment: str | None
    started_at: datetime | None
    url: str

    def body(self) -> bytes:
        return canonical_json(
            {
                "type": self.kind,
                "incident": {
                    "incident_id": self.incident_id,
                    "title": redact(self.title[:300]).text,
                    "severity": self.severity,
                    "service": self.service,
                    "environment": self.environment,
                    "started_at": self.started_at.isoformat() if self.started_at else None,
                    "url": self.url,
                },
            }
        )

    def digest(self) -> str:
        return hashlib.sha256(self.body()).hexdigest()


@dataclass(frozen=True)
class Channel:
    channel_id: str
    url: str
    secret: SecretStr | None = None


class Notifier(Protocol):
    async def send(self, channel: Channel, message: Message) -> None:
        """Deliver `message` or raise DeliveryFailed or NetguardBlocked."""
        ...


def signature(secret: SecretStr, timestamp: str, body: bytes) -> str:
    """The `X-Opsteam-Signature` value: HMAC-SHA256 over the timestamp, a dot and the body."""
    mac = hmac.new(secret.get_secret_value().encode(), timestamp.encode() + b"." + body, "sha256")
    return f"sha256={mac.hexdigest()}"


@dataclass(frozen=True)
class WebhookNotifier:
    """POSTs JSON to the vetted IP of the channel URL; the test hooks are transport and resolver."""

    clock: Clock
    dev_allow: frozenset[str] = frozenset()
    timeout_s: float = 5.0
    transport: httpx.AsyncBaseTransport | None = None
    resolver: Resolver | None = None
    verify: ssl.SSLContext | bool = True

    async def send(self, channel: Channel, message: Message) -> None:
        target = await check_url(channel.url, resolver=self.resolver, dev_allow=self.dev_allow)
        ip = target.ips[0]
        parts = urlsplit(channel.url)
        url = httpx.URL(
            scheme=target.scheme, host=ip, port=target.port, path=parts.path or "/",
            query=parts.query.encode(),
        )  # fmt: skip
        body = message.body()
        timestamp = str(int(self.clock.now().timestamp()))
        headers = {
            "Host": host_header(target),
            "Content-Type": "application/json",
            "User-Agent": "opsteam-notify",
            "X-Opsteam-Timestamp": timestamp,
        }
        if channel.secret is not None:
            headers["X-Opsteam-Signature"] = signature(channel.secret, timestamp, body)
        extensions: dict[str, Any] = {}
        if target.scheme == "https" and target.host != ip:
            extensions["sni_hostname"] = target.host
        # trust_env=False: an environment proxy would bypass the pinned address.
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout_s,
            follow_redirects=False,
            trust_env=False,
            verify=self.verify,
        ) as client:
            try:
                resp = await client.post(url, content=body, headers=headers, extensions=extensions)
            except httpx.TimeoutException as exc:
                raise DeliveryFailed("webhook did not answer in time") from exc
            except httpx.HTTPError as exc:
                raise DeliveryFailed("webhook could not be reached") from exc
        if not 200 <= resp.status_code < 300:
            raise DeliveryFailed(f"webhook returned HTTP {resp.status_code}")


@dataclass
class FakeNotifier:
    """Records deliveries; channels listed in `failing` raise DeliveryFailed."""

    sent: list[tuple[Channel, Message]] = field(default_factory=list)
    failing: set[str] = field(default_factory=set)

    async def send(self, channel: Channel, message: Message) -> None:
        if channel.channel_id in self.failing:
            raise DeliveryFailed("fake channel is down")
        self.sent.append((channel, message))
