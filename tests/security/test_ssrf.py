"""User-supplied URLs must never reach loopback, private, link-local or metadata addresses."""

import asyncio

import pytest
from opsteam_core.errors import NetguardBlocked
from opsteam_core.netguard import CheckedTarget, check_url

PUBLIC_IP = "93.184.215.14"


class FakeResolver:
    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers = answers
        self.calls = 0

    async def resolve(self, host: str, port: int) -> list[str]:
        self.calls += 1
        if host not in self.answers:
            raise OSError("NXDOMAIN")
        return self.answers[host]


RESOLVER = FakeResolver(
    {
        "example.com": [PUBLIC_IP],
        "metadata.google.internal": ["169.254.169.254"],
        "rebind.example": [PUBLIC_IP, "10.0.0.1"],
        "empty.example": [],
        "v6only.example": ["::1"],
    }
)


def _check(url: str, **kwargs: object) -> CheckedTarget:
    return asyncio.run(check_url(url, resolver=RESOLVER, **kwargs))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://127.0.0.1/",
        "https://127.1/",
        "https://2130706433/",
        "https://0x7f000001/",
        "https://017700000001/",
        "https://[::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://[::ffff:a9fe:a9fe]/",
        "https://[64:ff9b::a9fe:a9fe]/",
        "https://[2002:a9fe:a9fe::1]/",
        "https://10.1.2.3/",
        "https://172.16.0.1/",
        "https://192.168.1.1/",
        "https://100.64.0.1/",
        "https://[fc00::1]/",
        "https://[fe80::1]/",
        "https://224.0.0.1/",
        "https://224.0.1.1/",
        "https://0.0.0.0/",
        "https://v6only.example/",
        "https://rebind.example/",
        "https://empty.example/",
        "https://nxdomain.example/",
        "http://example.com/",
        "https://user:pw@example.com/",
        "https://example.com:22/",
        "https://example.com:99999/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "not a url",
        "https:///path-only",
    ],
)
def test_blocked(url: str) -> None:
    with pytest.raises(NetguardBlocked) as exc:
        _check(url)
    assert url not in str(exc.value)


def test_public_https_is_allowed_with_vetted_ips() -> None:
    target = _check("https://Example.com/api?x=1")
    assert (target.scheme, target.host, target.port, target.ips) == (
        "https",
        "example.com",
        443,
        (PUBLIC_IP,),
    )


def test_http_and_extra_ports_need_explicit_opt_in() -> None:
    assert _check("http://example.com/", allow_http=True).port == 80
    assert _check("https://example.com:9090/", extra_ports=frozenset({9090})).port == 9090


def test_ip_literal_skips_dns() -> None:
    before = RESOLVER.calls
    assert _check(f"https://{PUBLIC_IP}/").ips == (PUBLIC_IP,)
    assert RESOLVER.calls == before


DEV_ALLOW = frozenset({"127.0.0.1:30090"})


def test_dev_allow_admits_exactly_the_listed_ip_and_port() -> None:
    target = _check("http://127.0.0.1:30090/api/v1/query", dev_allow=DEV_ALLOW)
    assert (target.host, target.port, target.ips) == ("127.0.0.1", 30090, ("127.0.0.1",))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:30091",  # other port
        "http://127.0.0.2:30090",  # other host
        "http://localhost:30090",  # names are not matched, only IP literals
        "http://user:pw@127.0.0.1:30090",  # credentials still refused
        "ftp://127.0.0.1:30090",  # scheme still checked
        "http://169.254.169.254",
    ],
)
def test_dev_allow_does_not_widen(url: str) -> None:
    with pytest.raises(NetguardBlocked):
        _check(url, dev_allow=DEV_ALLOW)
