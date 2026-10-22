from typing import Any

import pytest
from opsteam_core.context import RequestContext
from pydantic import ValidationError

TENANT = "tnt_01JBQ2Z8Y3K4M5N6P7Q8R9S0T1"
USER = "usr_01JBQ2Z8Y3K4M5N6P7Q8R9S0T1"


def _ctx(**overrides: Any) -> RequestContext:
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "principal": {"type": "user", "id": USER},
        "roles": ["viewer"],
        "scopes": ["incident:read"],
    }
    return RequestContext.model_validate(fields | overrides)


def test_builds_with_generated_request_id() -> None:
    ctx = _ctx()
    assert ctx.tenant_id == TENANT
    assert ctx.request_id
    assert ctx.request_id != _ctx().request_id


def test_is_immutable() -> None:
    ctx = _ctx()
    with pytest.raises(ValidationError):
        ctx.tenant_id = "tnt_01JBQ2Z8Y3K4M5N6P7Q8R9S0T2"  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_id": "acme"},
        {"tenant_id": "usr_01JBQ2Z8Y3K4M5N6P7Q8R9S0T1"},
        {"incident_id": "evt_01JBQ2Z8Y3K4M5N6P7Q8R9S0T1"},
        {"principal": {"type": "user", "id": "rnr_01JBQ2Z8Y3K4M5N6P7Q8R9S0T1"}},
        {"principal": {"type": "runner", "id": USER}},
        {"principal": {"type": "admin", "id": USER}},
        {"unexpected": "x"},
    ],
)
def test_rejects_invalid_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _ctx(**overrides)


def test_system_principal_takes_a_plain_name() -> None:
    assert _ctx(principal={"type": "system", "id": "ingest"}).principal.id == "ingest"
