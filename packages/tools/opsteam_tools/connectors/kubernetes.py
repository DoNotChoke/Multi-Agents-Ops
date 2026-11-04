"""Read-only Kubernetes API client: a bearer token, the cluster's own CA, vetted addresses only."""

import ssl
from dataclasses import dataclass
from typing import Any

import httpx
from opsteam_core.netguard import Resolver, check_url
from pydantic import SecretStr

from opsteam_tools.connectors.http import ConnectorError, get_json

KUBERNETES_PORTS = frozenset({6443})


def trust(ca_cert: str) -> ssl.SSLContext:
    """A TLS context that trusts only the cluster CA in `ca_cert` (PEM)."""
    # An empty cadata would silently fall back to the system's public CAs.
    if not ca_cert.strip():
        raise ConnectorError("ca_cert is not a valid PEM certificate")
    try:
        return ssl.create_default_context(cadata=ca_cert)
    except (ssl.SSLError, ValueError) as exc:
        raise ConnectorError("ca_cert is not a valid PEM certificate") from exc


@dataclass(frozen=True)
class KubernetesConnector:
    """One integration's API server; `transport` and `resolver` exist for tests."""

    endpoint: str
    token: SecretStr
    ca_cert: str
    dev_allow: frozenset[str] = frozenset()
    timeout_s: float = 10.0
    max_bytes: int = 4 << 20
    transport: httpx.AsyncBaseTransport | None = None
    resolver: Resolver | None = None

    async def version(self) -> dict[str, Any]:
        """The server's build info; proves TLS and reachability, not the token's rights."""
        return await self._get("/version", {})

    async def pods(self, limit: int) -> list[dict[str, Any]]:
        """Pods across namespaces, at most `limit`; needs the token's list right."""
        items, _ = await self.items("/api/v1/pods", {"limit": str(limit)})
        return items

    async def items(self, path: str, params: dict[str, str]) -> tuple[list[dict[str, Any]], bool]:
        """A list call's objects, and whether the server holds more than it returned."""
        payload = await self._get(path, params)
        items = payload.get("items")
        if not isinstance(items, list):
            raise ConnectorError("kubernetes returned an unexpected shape")
        meta = payload.get("metadata")
        more = isinstance(meta, dict) and bool(meta.get("continue"))
        return [i for i in items if isinstance(i, dict)], more

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        target = await check_url(
            self.endpoint,
            extra_ports=KUBERNETES_PORTS,
            resolver=self.resolver,
            dev_allow=self.dev_allow,
        )
        payload = await get_json(
            "kubernetes",
            self.endpoint,
            target,
            path,
            params,
            {"Authorization": f"Bearer {self.token.get_secret_value()}"},
            timeout_s=self.timeout_s,
            max_bytes=self.max_bytes,
            transport=self.transport,
            verify=trust(self.ca_cert),
        )
        if not isinstance(payload, dict):
            raise ConnectorError("kubernetes returned an unexpected shape")
        return payload
