import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from opsteam_policy.client import UNAVAILABLE, PolicyClient, bundle_version
from opsteam_tools.registry.metrics import QUERY_RANGE

pytestmark = pytest.mark.integration

OPA_URL = os.environ.get("OPSTEAM_OPA_URL", "http://127.0.0.1:8181")
PATH = "opsteam/tool_access/decision"
VERSION = bundle_version(Path(__file__).parents[2] / "policies")


def tool_input(**change: Any) -> dict[str, Any]:
    spec = QUERY_RANGE
    body: dict[str, Any] = {
        "principal": {"tenant_id": "tnt_a", "type": "agent", "scopes": []},
        "agent": "metrics",
        "tool": spec.name,
        "tool_spec": {
            "permission": spec.permission,
            "allowed_agents": sorted(spec.allowed_agents),
            "max_window_seconds": spec.limits.max_window_s,
        },
        "integration": {"tenant_id": "tnt_a", "type": "prometheus", "status": "active"},
        "resource": {"service": "checkout", "environment": "shop"},
        "args": {"window_seconds": 3600},
    }
    return body | change


@pytest.fixture
async def opa() -> AsyncIterator[PolicyClient]:
    # A cold OPA can exceed the production 200 ms budget on its first query.
    client = PolicyClient(OPA_URL, VERSION, timeout=2.0)
    yield client
    await client.aclose()


async def test_registry_spec_is_allowed(opa: PolicyClient) -> None:
    d = await opa.decide(PATH, tool_input())
    assert (d.allow, d.reasons, d.obligations) == (True, (), {"max_window_seconds": 21600})
    assert d.policy_version == VERSION


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"integration": {"tenant_id": "tnt_b", "type": "prometheus", "status": "active"}},
         "cross_tenant"),
        ({"agent": "logs"}, "tool_not_allowed_for_agent"),
        ({"args": {"window_seconds": 6 * 3600 + 1}}, "window_too_large"),
        ({"tool_spec": None}, "invalid_input"),
    ],
)  # fmt: skip
async def test_real_policy_denies(opa: PolicyClient, change: dict[str, Any], reason: str) -> None:
    d = await opa.decide(PATH, tool_input(**change))
    assert not d.allow and reason in d.reasons


async def test_unknown_policy_path_is_a_deny(opa: PolicyClient) -> None:
    d = await opa.decide("opsteam/no_such_policy/decision", tool_input())
    assert (d.allow, d.reasons) == (False, (UNAVAILABLE,))
