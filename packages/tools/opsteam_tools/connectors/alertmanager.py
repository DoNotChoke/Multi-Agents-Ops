"""Read-only Alertmanager API v2 client: the alerts it would notify about right now."""

import ssl
from dataclasses import dataclass
from typing import Any

import httpx
from opsteam_core.netguard import Resolver, check_url

from opsteam_tools.connectors.http import ConnectorError, get_json

ALERTMANAGER_PORTS = frozenset({9093})
# Silenced and inhibited alerts are never sent, so they are not gaps.
_SENDABLE = {"active": "true", "silenced": "false", "inhibited": "false"}


@dataclass(frozen=True)
class AlertmanagerConnector:
    """One integration's Alertmanager; `transport`, `resolver` and `verify` exist for tests."""

    endpoint: str
    dev_allow: frozenset[str] = frozenset()
    timeout_s: float = 10.0
    max_bytes: int = 4 << 20
    transport: httpx.AsyncBaseTransport | None = None
    resolver: Resolver | None = None
    verify: ssl.SSLContext | bool = True

    async def alerts(self) -> list[dict[str, Any]]:
        """Active alerts that are neither silenced nor inhibited, as Alertmanager returns them."""
        target = await check_url(
            self.endpoint,
            extra_ports=ALERTMANAGER_PORTS,
            resolver=self.resolver,
            dev_allow=self.dev_allow,
        )
        payload = await get_json(
            "alertmanager",
            self.endpoint,
            target,
            "/api/v2/alerts",
            _SENDABLE,
            {},
            timeout_s=self.timeout_s,
            max_bytes=self.max_bytes,
            transport=self.transport,
            verify=self.verify,
        )
        if not isinstance(payload, list):
            raise ConnectorError("alertmanager returned an unexpected shape")
        return [a for a in payload if isinstance(a, dict)]
