"""Read-only Prometheus HTTP API client that connects only to addresses netguard has vetted."""

import ssl
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from opsteam_core.netguard import CheckedTarget, Resolver, check_url
from pydantic import SecretStr

from opsteam_tools.connectors.http import (
    ConnectorError,
    ConnectorTimeout,
    ResponseTooLarge,
    get_json,
)

__all__ = [
    "PROMETHEUS_PORTS",
    "ConnectorError",
    "ConnectorTimeout",
    "PrometheusConnector",
    "ResponseTooLarge",
]

# Prometheus listens on 9090 by default; anything else must sit behind 80/443.
PROMETHEUS_PORTS = frozenset({9090})


@dataclass(frozen=True)
class PrometheusConnector:
    """One integration's Prometheus; `transport`, `resolver` and `verify` exist for tests."""

    endpoint: str
    token: SecretStr | None = None
    dev_allow: frozenset[str] = frozenset()
    timeout_s: float = 10.0
    max_bytes: int = 1 << 20
    transport: httpx.AsyncBaseTransport | None = None
    resolver: Resolver | None = None
    verify: ssl.SSLContext | bool = True

    async def query(self, promql: str, at: datetime | None = None) -> Any:
        """Instant query; returns Prometheus' `data` object."""
        params = {"query": promql} | ({"time": str(at.timestamp())} if at else {})
        return await self._get("/api/v1/query", params)

    async def query_range(self, promql: str, start: datetime, end: datetime, step_s: int) -> Any:
        """Range query; returns Prometheus' `data` object."""
        params = {
            "query": promql,
            "start": str(start.timestamp()),
            "end": str(end.timestamp()),
            "step": str(step_s),
        }
        return await self._get("/api/v1/query_range", params)

    async def alerts(self) -> Any:
        """Active alerts known to this Prometheus."""
        return (await self._get("/api/v1/alerts", {}))["alerts"]

    async def build_info(self) -> Any:
        """Version information; a cheap read used to test connectivity."""
        return await self._get("/api/v1/status/buildinfo", {})

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        target = await check_url(
            self.endpoint,
            extra_ports=PROMETHEUS_PORTS,
            resolver=self.resolver,
            dev_allow=self.dev_allow,
        )
        return await self._request(target, path, params)

    async def _request(self, target: CheckedTarget, path: str, params: dict[str, str]) -> Any:
        headers = {}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token.get_secret_value()}"
        payload = await get_json(
            "prometheus",
            self.endpoint,
            target,
            path,
            params,
            headers,
            timeout_s=self.timeout_s,
            max_bytes=self.max_bytes,
            transport=self.transport,
            verify=self.verify,
        )
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ConnectorError("prometheus reported an error")
        return payload.get("data")
