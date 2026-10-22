"""Typed exceptions for expected failures."""

from typing import ClassVar


class OpsteamError(Exception):
    """Base class for expected failures; `code` is a stable identifier for APIs and logs."""

    code: ClassVar[str] = "opsteam.error"


class InvalidId(OpsteamError, ValueError):
    """An identifier is not a well-formed `<prefix>_<ULID>` of the expected kind."""

    code = "id.invalid"


class RedactionFailed(OpsteamError):
    """Content could not be redacted, so it must not be stored, shown or sent to an LLM."""

    code = "redaction.failed"


class UnknownRole(OpsteamError):
    """A membership carries a role that maps to no scopes."""

    code = "authz.unknown_role"
