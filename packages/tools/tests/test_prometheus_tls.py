"""Real TLS: the pinned IP is dialled while SNI and the certificate check use the hostname."""

import asyncio
import datetime as dt
import ssl
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from opsteam_core.netguard import CheckedTarget
from opsteam_tools.connectors.prometheus import ConnectorError, PrometheusConnector

BODY = b'{"status":"success","data":{"version":"3.15.1"}}'


def _self_signed(tmp: Path, name: str) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp / "cert.pem", tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
async def server(tmp_path: Path) -> AsyncIterator[tuple[int, Path, list[str | None]]]:
    cert, key = _self_signed(tmp_path, "prom.test")
    seen_sni: list[str | None] = []
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    ctx.sni_callback = lambda _sock, name, _ctx: seen_sni.append(name)  # type: ignore[assignment,func-returns-value]

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(BODY)}\r\nConnection: close\r\n\r\n".encode()
            + BODY
        )
        await writer.drain()
        writer.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    port = srv.sockets[0].getsockname()[1]
    yield port, cert, seen_sni
    srv.close()


def _client(cert: Path) -> PrometheusConnector:
    trust = ssl.create_default_context(cafile=str(cert))
    return PrometheusConnector("https://prom.test", verify=trust)


async def test_pinned_ip_with_sni_verifies_the_certificate(
    server: tuple[int, Path, list[str | None]],
) -> None:
    port, cert, seen_sni = server
    target = CheckedTarget("https", "prom.test", port, ("127.0.0.1",))
    data = await _client(cert)._request(target, "/api/v1/status/buildinfo", {})
    assert data == {"version": "3.15.1"}
    assert seen_sni == ["prom.test"]


async def test_certificate_for_another_name_is_refused(
    server: tuple[int, Path, list[str | None]],
) -> None:
    port, cert, _ = server
    target = CheckedTarget("https", "evil.test", port, ("127.0.0.1",))
    with pytest.raises(ConnectorError, match="could not be reached"):
        await _client(cert)._request(target, "/api/v1/status/buildinfo", {})
