"""Read-only Loki HTTP API client that connects only to addresses netguard has vetted."""

import ssl
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from opsteam_core.netguard import Resolver, check_url
from pydantic import SecretStr

from opsteam_tools.connectors.http import ConnectorError, get_json

LOKI_PORTS = frozenset({3100})


@dataclass(frozen=True)
class LokiConnector:
    """One integration's Loki; `transport`, `resolver` and `verify` exist for tests."""

    endpoint: str
    token: SecretStr | None = None
    dev_allow: frozenset[str] = frozenset()
    timeout_s: float = 10.0
    max_bytes: int = 1 << 20
    transport: httpx.AsyncBaseTransport | None = None
    resolver: Resolver | None = None
    verify: ssl.SSLContext | bool = True

    async def labels(self) -> list[str]:
        """Known label names; a cheap read used to test connectivity."""
        data = await self._get("/loki/api/v1/labels", {})
        if not isinstance(data, list):
            raise ConnectorError("loki returned an unexpected shape")
        return [str(name) for name in data]

    async def query_range(
        self,
        logql: str,
        start_ns: int,
        end_ns: int,
        *,
        limit: int | None = None,
        step_s: int | None = None,
        direction: Literal["backward", "forward"] = "backward",
    ) -> dict[str, Any]:
        """Log lines (`streams`) or, for a metric query, series (`matrix`) between two instants."""
        params = {"query": logql, "start": str(start_ns), "end": str(end_ns)}
        params["direction"] = direction
        if limit is not None:
            params["limit"] = str(limit)
        if step_s is not None:
            params["step"] = str(step_s)
        data = await self._get("/loki/api/v1/query_range", params)
        if not isinstance(data, dict) or not isinstance(data.get("result"), list):
            raise ConnectorError("loki returned an unexpected shape")
        return data

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        target = await check_url(
            self.endpoint, extra_ports=LOKI_PORTS, resolver=self.resolver, dev_allow=self.dev_allow
        )
        headers = {}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token.get_secret_value()}"
        payload = await get_json(
            "loki",
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
            raise ConnectorError("loki returned an unexpected shape")
        return payload.get("data")
