"""Prefixed, time-ordered identifiers such as `inc_01JBQ2Z8Y3K4M5N6P7Q8R9S0T1`."""

import re
from typing import Literal

from ulid import ULID

from opsteam_core.errors import InvalidId

type IdPrefix = Literal[
    "tnt", "usr", "int", "svc", "rule", "evt", "inc", "tsk", "tc",
    "run", "ev", "fnd", "hyp", "plan", "apr", "aud", "rnr", "llm", "sec",
]  # fmt: skip

_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


def new_id(prefix: IdPrefix) -> str:
    """Return a new ID with the given prefix; IDs created later sort after earlier ones."""
    return f"{prefix}_{ULID()}"


def parse_id(value: str, prefix: IdPrefix) -> str:
    """Return `value` unchanged if it is a well-formed ID with `prefix`, else raise InvalidId."""
    head, sep, body = value.partition("_")
    if head != prefix or not sep or not _ULID.fullmatch(body):
        raise InvalidId(f"malformed {prefix} id")
    return value
