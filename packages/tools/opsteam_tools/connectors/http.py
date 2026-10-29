"""One read-only GET to a customer system, sent only to the address netguard vetted."""

import json
import ssl
from typing import Any
from urllib.parse import urlsplit

import httpx
from opsteam_core.errors import OpsteamError
from opsteam_core.netguard import CheckedTarget, host_header


class ConnectorError(OpsteamError):
    """The data source could not be queried; the message never includes the response body."""

    code = "connector.failed"


class ConnectorTimeout(ConnectorError):
    code = "connector.timeout"


class ResponseTooLarge(ConnectorError):
    code = "connector.too_large"


async def get_json(
    what: str,
    endpoint: str,
    target: CheckedTarget,
    path: str,
    params: dict[str, str],
    headers: dict[str, str],
    *,
    timeout_s: float,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None,
    verify: ssl.SSLContext | bool,
) -> Any:
    """GET `path` under `endpoint` at `target`'s vetted IP while TLS and Host keep the name.

    Returns the parsed JSON of a 200; `what` names the system in error messages.
    """
    ip = target.ips[0]
    prefix = urlsplit(endpoint).path.rstrip("/")
    url = httpx.URL(
        scheme=target.scheme, host=ip, port=target.port, path=prefix + path, params=params
    )
    headers = {"Host": host_header(target)} | headers
    extensions: dict[str, Any] = {}
    if target.scheme == "https" and target.host != ip:
        extensions["sni_hostname"] = target.host  # also the name the certificate must match

    # trust_env=False: an environment proxy would bypass the pinned address.
    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout_s,
        follow_redirects=False,
        trust_env=False,
        verify=verify,
    ) as client:
        try:
            async with client.stream("GET", url, headers=headers, extensions=extensions) as resp:
                if resp.is_redirect:
                    raise ConnectorError(f"{what} answered with a redirect; not followed")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise ResponseTooLarge(f"{what} response exceeds {max_bytes} bytes")
                status = resp.status_code
        except httpx.TimeoutException as exc:
            raise ConnectorTimeout(f"{what} did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise ConnectorError(f"{what} could not be reached") from exc
    if status != 200:
        raise ConnectorError(f"{what} returned HTTP {status}")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ConnectorError(f"{what} returned invalid JSON") from exc
