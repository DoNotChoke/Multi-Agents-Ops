import pytest
from opsteam_core.authz import ROLE_SCOPES, has_scope, scopes_for
from opsteam_core.errors import UnknownRole

ROLES = ["viewer", "responder", "approver", "admin", "owner"]

# (role, scope, granted)
TABLE = [
    ("viewer", "incident:read", True),
    ("viewer", "catalog:read", True),
    ("viewer", "usage:read", True),
    ("viewer", "investigation:create", False),
    ("viewer", "integration:write", False),
    ("responder", "investigation:create", True),
    ("responder", "incident:followup", True),
    ("responder", "incident:comment", True),
    ("responder", "action:approve", False),
    ("approver", "action:approve", True),
    ("approver", "integration:write", False),
    ("admin", "incident:read", True),
    ("admin", "integration:write", True),
    ("admin", "runner:revoke", True),
    ("admin", "policy:write", True),
    ("admin", "catalog:write", True),
    ("admin", "member:manage", True),
    ("admin", "audit:read", True),
    ("admin", "action:approve", False),
    ("admin", "member:manage_owner", False),
    ("admin", "tenant:billing", False),
    ("owner", "member:manage_owner", True),
    ("owner", "tenant:billing", True),
    ("owner", "action:approve", False),
]


@pytest.mark.parametrize(("role", "scope", "granted"), TABLE)
def test_role_scope_table(role: str, scope: str, granted: bool) -> None:
    assert has_scope(scopes_for([role]), scope) is granted


def test_every_role_is_covered() -> None:
    assert set(ROLE_SCOPES) == set(ROLES)


def test_roles_combine() -> None:
    assert has_scope(scopes_for(["approver", "admin"]), "action:approve")


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(UnknownRole):
        scopes_for(["viewer", "superuser"])


def test_wildcard_is_not_a_prefix_match() -> None:
    assert not has_scope(frozenset({"integration:*"}), "integrations:write")
    assert not has_scope(frozenset({"integration:read"}), "integration:*")
