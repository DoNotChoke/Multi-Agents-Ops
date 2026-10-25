import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from opsteam_policy.client import UNAVAILABLE, PolicyClient, bundle_version

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]
PATH = "opsteam/tool_access/decision"
ALLOW = {"allow": True, "reasons": [], "obligations": {"max_window_seconds": 21600}}


def answer(body: object, status: int = 200) -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return handler


async def decide(handler: Handler, budget: float = 0.2) -> tuple[bool, tuple[str, ...]]:
    client = PolicyClient(
        "http://opa:8181", "v1", timeout=budget, transport=httpx.MockTransport(handler)
    )
    try:
        d = await client.decide(PATH, {"tool": "metrics.query_range"})
    finally:
        await client.aclose()
    return d.allow, d.reasons


async def test_allow_is_passed_through_with_version() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": ALLOW})

    client = PolicyClient("http://opa:8181", "v1", transport=httpx.MockTransport(handler))
    d = await client.decide(PATH, {"tool": "metrics.query_range"})
    await client.aclose()
    assert (d.allow, d.reasons, d.obligations, d.policy_version) == (
        True, (), {"max_window_seconds": 21600}, "v1",
    )  # fmt: skip
    assert seen[0].url.path == f"/v1/data/{PATH}"
    assert seen[0].read() == b'{"input":{"tool":"metrics.query_range"}}'


async def test_policy_deny_keeps_its_reasons() -> None:
    body = {"result": {"allow": False, "reasons": ["cross_tenant"], "obligations": {}}}
    assert await decide(answer(body)) == (False, ("cross_tenant",))


async def _slow(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(1)
    return httpx.Response(200, json={"result": ALLOW})


async def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


async def _not_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"{not json")


@pytest.mark.parametrize(
    "handler",
    [
        _slow,
        _refused,
        _not_json,
        answer({"result": ALLOW}, status=500),
        answer({}),  # undefined policy path: OPA omits "result"
        answer({"result": {**ALLOW, "allow": "true"}}),
        answer({"result": {"allow": True}}),
        answer({"result": {**ALLOW, "extra": 1}}),
        answer({"result": {**ALLOW, "obligations": {"max_window_seconds": [1]}}}),
        answer([ALLOW]),
    ],
    ids=["slow", "refused", "not_json", "http_500", "no_result", "string_allow",
         "missing_fields", "extra_field", "odd_obligation", "wrong_shape"],
)  # fmt: skip
async def test_anything_unclear_is_a_deny(handler: Handler) -> None:
    assert await decide(handler, budget=0.05) == (False, (UNAVAILABLE,))


async def test_bad_path_is_refused_before_any_call() -> None:
    client = PolicyClient("http://opa:8181", "v1", transport=httpx.MockTransport(_refused))
    with pytest.raises(ValueError):
        await client.decide("opsteam/../admin", {})
    await client.aclose()


def test_bundle_version_tracks_policies_not_tests(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "a.rego").write_text("package a\n")
    (tmp_path / "tests" / "a_test.rego").write_text("package a_test\n")
    first = bundle_version(tmp_path)
    (tmp_path / "tests" / "a_test.rego").write_text("package a_test\n# changed\n")
    assert bundle_version(tmp_path) == first
    (tmp_path / "a.rego").write_text("package a\n# changed\n")
    assert bundle_version(tmp_path) != first
