"""Vet user-supplied outbound URLs so they cannot reach internal or metadata addresses (SSRF)."""

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from opsteam_core.errors import NetguardBlocked

DEFAULT_PORTS = frozenset({80, 443})
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_NUMERIC_LABEL = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)")


class Resolver(Protocol):
    async def resolve(self, host: str, port: int) -> list[str]:
        """Return every IP address `host` resolves to."""
        ...


class SystemResolver:
    """Resolver backed by the operating system's getaddrinfo."""

    async def resolve(self, host: str, port: int) -> list[str]:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return [str(info[4][0]) for info in infos]


# TODO(T-1.5): connectors must connect only to `ips` (keeping SNI) and refuse redirects.
@dataclass(frozen=True)
class CheckedTarget:
    """A URL that passed every check, with the vetted IPs the client must connect to."""

    scheme: str
    host: str
    port: int
    ips: tuple[str, ...]


async def check_url(
    url: str,
    *,
    allow_http: bool = False,
    extra_ports: frozenset[int] = frozenset(),
    resolver: Resolver | None = None,
) -> CheckedTarget:
    """Return the vetted target for `url` or raise NetguardBlocked; DNS is resolved only once."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme != "https" and not (allow_http and scheme == "http"):
        raise NetguardBlocked("scheme not allowed")
    if parts.username is not None or parts.password is not None:
        raise NetguardBlocked("credentials in URL")
    host = parts.hostname
    if not host:
        raise NetguardBlocked("missing host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise NetguardBlocked("invalid port") from exc
    if port not in DEFAULT_PORTS | extra_ports:
        raise NetguardBlocked("port not allowed")

    ips = [host] if _is_ip(host) else await _resolve(host, port, resolver or SystemResolver())
    if not ips or not all(_is_public(ip) for ip in ips):
        raise NetguardBlocked("host resolves to a non-public address")
    return CheckedTarget(scheme, host, port, tuple(ips))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if all(_NUMERIC_LABEL.fullmatch(label) for label in host.split(".")):
            # 127.1, 2130706433, 0x7f000001: resolvers disagree on these, so refuse them.
            raise NetguardBlocked("ambiguous numeric host") from None
        return False
    return True


async def _resolve(host: str, port: int, resolver: Resolver) -> list[str]:
    try:
        return await resolver.resolve(host, port)
    except OSError as exc:
        raise NetguardBlocked("host does not resolve") from exc


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = addr.ipv4_mapped or addr.sixtofour or (addr.teredo[1] if addr.teredo else None)
        if any(addr in net for net in _NAT64):
            embedded = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        if embedded is not None and not _is_public(str(embedded)):
            return False
    return addr.is_global and not addr.is_multicast
