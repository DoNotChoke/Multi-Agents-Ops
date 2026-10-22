"""Role to scope mapping, the single source for what each tenant role may do."""

from collections.abc import Iterable, Mapping

from opsteam_core.errors import UnknownRole

_VIEWER = frozenset({"incident:read", "catalog:read", "usage:read"})
_RESPONDER = _VIEWER | {"investigation:create", "incident:followup", "incident:comment"}
_APPROVER = _RESPONDER | {"action:approve"}
# Admins do not approve actions: approval stays a separate, explicit role.
_ADMIN = _RESPONDER | {
    "integration:*",
    "runner:*",
    "policy:*",
    "catalog:*",
    "member:manage",
    "audit:read",
}
_OWNER = _ADMIN | {"member:manage_owner", "tenant:billing"}

ROLE_SCOPES: Mapping[str, frozenset[str]] = {
    "viewer": _VIEWER,
    "responder": _RESPONDER,
    "approver": _APPROVER,
    "admin": _ADMIN,
    "owner": _OWNER,
}


def scopes_for(roles: Iterable[str]) -> frozenset[str]:
    """Return the union of scopes granted by `roles`; raise UnknownRole for any unmapped role."""
    granted: frozenset[str] = frozenset()
    for role in roles:
        if role not in ROLE_SCOPES:
            raise UnknownRole("membership has an unknown role")
        granted |= ROLE_SCOPES[role]
    return granted


def has_scope(granted: frozenset[str], required: str) -> bool:
    """Return True if `required` is granted directly or through a `<resource>:*` wildcard."""
    resource, _, _ = required.partition(":")
    return required in granted or f"{resource}:*" in granted
