"""Canonical JSON (RFC 8785 / JCS) for every hash and signature (docs/04 §4, ADR-0014).

Verifiers canonicalize the JSON text exactly as received (`canonicalize_text`), never a
re-serialized model, so timestamps and numbers are hashed as they were sent.
"""

import json

import rfc8785

type JsonValue = dict[str, JsonValue] | list[JsonValue] | str | int | float | bool | None


class CanonicalizationError(ValueError):
    """Value has no canonical form: NaN/Infinity, |int| > 2**53-1, duplicate key, bad text."""


def canonical_json(value: JsonValue) -> bytes:
    try:
        return rfc8785.dumps(value)
    except rfc8785.CanonicalizationError as exc:
        # Fixed message: the library's message echoes the offending value (INV-05).
        raise CanonicalizationError("value has no RFC 8785 canonical form") from exc


def canonicalize_text(raw: str | bytes) -> bytes:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicates, parse_constant=_reject)
    except CanonicalizationError:
        raise
    except ValueError as exc:
        raise CanonicalizationError("invalid JSON text") from exc
    return canonical_json(value)


def _reject_duplicates(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise CanonicalizationError("duplicate object key")
    return result


def _reject(_: str) -> JsonValue:
    raise CanonicalizationError("NaN and Infinity are not JSON")
