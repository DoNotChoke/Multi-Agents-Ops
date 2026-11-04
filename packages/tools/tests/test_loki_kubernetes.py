import datetime as dt
from collections.abc import Callable

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from opsteam_core.errors import NetguardBlocked
from opsteam_tools.connectors.http import ConnectorError
from opsteam_tools.connectors.kubernetes import KubernetesConnector, trust
from opsteam_tools.connectors.loki import LokiConnector
from pydantic import SecretStr

PUBLIC_IP = "93.184.216.34"
TOKEN = SecretStr("dev-only-k8s-reader-token")


class Resolver:
    async def resolve(self, host: str, port: int) -> list[str]:
        return [PUBLIC_IP]


def _ca() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kubernetes")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _recording(
    body: object, seen: list[httpx.Request]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    return handler


async def test_loki_labels_from_the_vetted_ip() -> None:
    seen: list[httpx.Request] = []
    body = {"status": "success", "data": ["app", "namespace"]}
    loki = LokiConnector(
        "https://loki.example.com",
        transport=httpx.MockTransport(_recording(body, seen)),
        resolver=Resolver(),
    )
    assert await loki.labels() == ["app", "namespace"]
    (request,) = seen
    assert (request.url.host, request.url.path) == (PUBLIC_IP, "/loki/api/v1/labels")
    assert request.headers["host"] == "loki.example.com"
    assert "authorization" not in request.headers


async def test_loki_rejects_an_unexpected_answer() -> None:
    loki = LokiConnector(
        "https://loki.example.com",
        transport=httpx.MockTransport(_recording({"status": "error"}, [])),
        resolver=Resolver(),
    )
    with pytest.raises(ConnectorError, match="unexpected shape"):
        await loki.labels()


async def test_loki_refuses_internal_addresses() -> None:
    with pytest.raises(NetguardBlocked):
        await LokiConnector("http://127.0.0.1:30100").labels()
    # The lab's exact address passes only when dev allows it.
    seen: list[httpx.Request] = []
    body = {"status": "success", "data": []}
    loki = LokiConnector(
        "http://127.0.0.1:30100",
        dev_allow=frozenset({"127.0.0.1:30100"}),
        transport=httpx.MockTransport(_recording(body, seen)),
    )
    assert await loki.labels() == []


async def test_kubernetes_sends_the_token_and_reads_pods() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/version":
            return httpx.Response(200, json={"gitVersion": "v1.35.0"})
        return httpx.Response(200, json={"kind": "PodList", "items": [{"metadata": {}}]})

    k8s = KubernetesConnector(
        "https://k8s.example.com:6443",
        TOKEN,
        _ca(),
        transport=httpx.MockTransport(handler),
        resolver=Resolver(),
    )
    assert (await k8s.version())["gitVersion"] == "v1.35.0"
    assert len(await k8s.pods(limit=1)) == 1
    assert [(r.url.path, r.url.params.get("limit")) for r in seen] == [
        ("/version", None),
        ("/api/v1/pods", "1"),
    ]
    assert {r.url.host for r in seen} == {PUBLIC_IP}
    assert all(r.headers["authorization"] == f"Bearer {TOKEN.get_secret_value()}" for r in seen)


async def test_kubernetes_forbidden_is_a_connector_error() -> None:
    k8s = KubernetesConnector(
        "https://k8s.example.com:6443",
        TOKEN,
        _ca(),
        transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"secret": "x"})),
        resolver=Resolver(),
    )
    with pytest.raises(ConnectorError, match="HTTP 403") as caught:
        await k8s.pods(limit=1)
    assert TOKEN.get_secret_value() not in str(caught.value)


def test_trust_takes_only_a_real_certificate() -> None:
    assert trust(_ca()).verify_mode.name == "CERT_REQUIRED"
    with pytest.raises(ConnectorError, match="not a valid PEM"):
        trust("-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----\n")
    with pytest.raises(ConnectorError):
        trust("")
