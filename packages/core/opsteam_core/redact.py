"""Remove secrets and personal data from text and JSON before it is stored, logged or sent out."""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from opsteam_core.canonical import JsonValue
from opsteam_core.errors import RedactionFailed

_KEY_WORDS = r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key)"
_HEADER_WORDS = r"(?:proxy-authorization|authorization|set-cookie|cookie)"
_SENSITIVE_KEY = re.compile(rf"(?i)[\w.-]*?(?:{_KEY_WORDS}|{_HEADER_WORDS})")
_MASKED_VALUE = "[REDACTED:secret]"


@dataclass(frozen=True)
class Redacted:
    """Redacted text plus how many items of each kind were removed (never the values)."""

    text: str
    counts: dict[str, int]


def _token_like(m: re.Match[str]) -> bool:
    # "Bearer authentication failed" is prose; real tokens carry digits or symbols.
    return any(c.isdigit() or c in "._~+/=-" for c in m.group("value"))


def _luhn_ok(digits: list[int]) -> bool:
    total = sum(
        d if i % 2 == 0 else (d * 2 - 9 if d > 4 else d * 2) for i, d in enumerate(reversed(digits))
    )
    return 13 <= len(digits) <= 19 and total % 10 == 0


def _pan_length(m: re.Match[str]) -> int:
    """Return the length of the longest Luhn-valid prefix of the match, or 0 if there is none."""
    # The match is greedy, so digits that follow a card ("... 0004 0") must not hide it.
    text, digits, best = m.group(0), [], 0
    for i, c in enumerate(text):
        if c.isdigit():
            digits.append(int(c))
            if _luhn_ok(digits):
                best = i + 1
    return best


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    accept: Callable[[re.Match[str]], bool] | None = None
    # When set, only the first N characters of the match are the secret; 0 means no secret.
    secret_length: Callable[[re.Match[str]], int] | None = None


# Order matters: specific formats first, generic key=value and PII last.
_RULES = (
    _Rule(
        "private_key",
        re.compile(
            r"-----BEGIN[A-Z0-9 ]* PRIVATE KEY-----"
            r"[\s\S]*?(?:-----END[A-Z0-9 ]* PRIVATE KEY-----|\Z)"
        ),
    ),
    _Rule("token", re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*")),
    _Rule(
        "token",
        re.compile(r"(?i)(?P<keep>\b(?:bearer|basic)\s+)(?P<value>[A-Za-z0-9._~+/=-]{8,})"),
        _token_like,
    ),
    _Rule("cloud_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b")),
    _Rule("cloud_key", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    _Rule("cloud_key", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b")),
    _Rule("cloud_key", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    _Rule("cloud_key", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    _Rule(
        "connection_string",
        # Scheme length is bounded: an unbounded run here is quadratic on long dotted text.
        re.compile(r"(?P<keep>\b[A-Za-z][A-Za-z0-9+.-]{0,31}://)[^\s/:@]*:[^\s/@]+(?=@)"),
    ),
    _Rule(
        "secret",
        re.compile(
            rf"(?im)(?P<keep>(?<![\w-]){_HEADER_WORDS}[\"']?\s*[:=]\s*"
            r"(?!\s*[\"']?\[REDACTED)(?P<q>[\"'])?)(?(q)[^\"'\r\n]+|[^\r\n]+)"
        ),
    ),
    _Rule(
        "secret",
        re.compile(
            rf"(?i)(?P<keep>(?<![\w.-])[\w.-]*?{_KEY_WORDS}[\"']?\s*[:=]\s*"
            r"(?!\s*[\"']?\[REDACTED)(?P<q>[\"'])?)(?(q)[^\"'\r\n]+|[^\s,;&\"'}]+)"
        ),
    ),
    _Rule("email", re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Not after "." either: the fraction of a float ("5.4111...") is not a card number.
    _Rule(
        "pan", re.compile(r"(?<![\d.-])[3-6](?:[ -]?\d){12,18}(?!\d)"), secret_length=_pan_length
    ),
)


def _apply(rule: _Rule, text: str) -> tuple[str, int]:
    hits = 0

    def replace(m: re.Match[str]) -> str:
        nonlocal hits
        if rule.accept is not None and not rule.accept(m):
            return m.group(0)
        if rule.secret_length is not None:
            n = rule.secret_length(m)
            if n == 0:
                return m.group(0)
            hits += 1
            return f"[REDACTED:{rule.kind}]" + m.group(0)[n:]
        hits += 1
        return (m.groupdict().get("keep") or "") + f"[REDACTED:{rule.kind}]"

    return rule.pattern.sub(replace, text), hits


def redact(text: str) -> Redacted:
    """Return `text` with each secret replaced by `[REDACTED:<kind>]`, plus a count per kind."""
    counts: Counter[str] = Counter()
    for rule in _RULES:
        text, hits = _apply(rule, text)
        if hits:
            counts[rule.kind] += hits
    return Redacted(text, dict(counts))


def redact_value(value: JsonValue) -> tuple[JsonValue, dict[str, int]]:
    """Redact every string in a JSON value and mask values under sensitive keys; fail closed."""
    counts: Counter[str] = Counter()
    try:
        return _walk(value, counts), dict(counts)
    except RecursionError as exc:
        raise RedactionFailed("value is nested too deeply to redact") from exc


def _walk(value: JsonValue, counts: Counter[str]) -> JsonValue:
    if isinstance(value, str):
        result = redact(value)
        counts.update(result.counts)
        return result.text
    if isinstance(value, list):
        return [_walk(item, counts) for item in value]
    if isinstance(value, dict):
        out: dict[str, JsonValue] = {}
        for key, item in value.items():
            if item is not None and _SENSITIVE_KEY.fullmatch(key):
                counts["secret"] += 1
                out[key] = _MASKED_VALUE
            else:
                out[key] = _walk(item, counts)
        return out
    return value
