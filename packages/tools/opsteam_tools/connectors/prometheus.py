"""Read-only Prometheus HTTP API client that connects only to addresses netguard has vetted."""

import json
import ssl
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from opsteam_core.errors import OpsteamError
from opsteam_core.netguard import CheckedTarget, Resolver, check_url
from pydantic import SecretStr

# Prometheus listens on 9090 by default; anything else must sit behind 80/443.
PROMETHEUS_PORTS = frozenset({9090})


class ConnectorError(OpsteamError):
    """The data source could not be queried; the message never includes the response body."""

    code = "connector.failed"


class ConnectorTimeout(ConnectorError):
    code = "connector.timeout"


class ResponseTooLarge(ConnectorError):
    code = "connector.too_large"


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
        """Send one GET to `target`'s vetted IP while TLS and Host still name the original host."""
        ip = target.ips[0]
        prefix = urlsplit(self.endpoint).path.rstrip("/")
        url = httpx.URL(
            scheme=target.scheme,
            host=ip,
            port=target.port,
            path=prefix + path,
            params=params,
        )
        headers = {"Host": _host_header(target)}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token.get_secret_value()}"
        extensions: dict[str, Any] = {}
        if target.scheme == "https" and target.host != ip:
            extensions["sni_hostname"] = target.host  # also the name the certificate must match

        # trust_env=False: an environment proxy would bypass the pinned address.
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout_s,
            follow_redirects=False,
            trust_env=False,
            verify=self.verify,
        ) as client:
            try:
                async with client.stream(
                    "GET", url, headers=headers, extensions=extensions
                ) as resp:
                    if resp.is_redirect:
                        raise ConnectorError("prometheus answered with a redirect; not followed")
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > self.max_bytes:
                            raise ResponseTooLarge(f"response exceeds {self.max_bytes} bytes")
                    status = resp.status_code
            except httpx.TimeoutException as exc:
                raise ConnectorTimeout("prometheus did not answer in time") from exc
            except httpx.HTTPError as exc:
                raise ConnectorError("prometheus could not be reached") from exc
        if status != 200:
            raise ConnectorError(f"prometheus returned HTTP {status}")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise ConnectorError("prometheus returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ConnectorError("prometheus reported an error")
        return payload.get("data")


def _host_header(target: CheckedTarget) -> str:
    default = 443 if target.scheme == "https" else 80
    host = f"[{target.host}]" if ":" in target.host else target.host
    return host if target.port == default else f"{host}:{target.port}"
