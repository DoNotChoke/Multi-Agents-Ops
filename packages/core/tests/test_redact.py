import json
import time
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from opsteam_core.errors import RedactionFailed
from opsteam_core.redact import redact, redact_value

SECRETS: list[dict[str, Any]] = [
    json.loads(line)
    for line in (
        Path(__file__).resolve().parents[3] / "tests/security/redaction_corpus/secrets.jsonl"
    )
    .read_text(encoding="utf-8")
    .splitlines()
    if line.strip()
]
filler = st.text(st.characters(whitelist_categories=["Lu", "Ll", "Nd", "Zs"]), max_size=40)


@given(filler, st.sampled_from(SECRETS), filler)
def test_secret_inside_random_text_is_removed(
    before: str, case: dict[str, Any], after: str
) -> None:
    result = redact(f"{before} {case['text']} {after}")
    assert case["secret"] not in result.text
    assert case["secret"] not in str(result.counts)


@given(st.one_of(st.text(), st.sampled_from([c["text"] for c in SECRETS])))
def test_redact_is_idempotent(text: str) -> None:
    once = redact(text).text
    assert redact(once).text == once


@pytest.mark.parametrize("unit", ["a.", "a-", "a@", "x:y", "4 ", "password", '"', "cookie:"])
def test_redact_stays_linear_on_adversarial_input(unit: str) -> None:
    started = time.perf_counter()
    redact(unit * (200_000 // len(unit)))
    assert time.perf_counter() - started < 2.0


def test_redact_value_masks_sensitive_keys_and_strings() -> None:
    value, counts = redact_value(
        {
            "password": 123,
            "nested": [{"api_key": "fake-key"}, {"refresh_token": None}],
            "msg": "login password=hunter2fake",
            "token_count": 12,
        }
    )
    assert value == {
        "password": "[REDACTED:secret]",
        "nested": [{"api_key": "[REDACTED:secret]"}, {"refresh_token": None}],
        "msg": "login password=[REDACTED:secret]",
        "token_count": 12,
    }
    assert counts == {"secret": 3}


def test_redact_value_fails_closed_on_pathological_nesting() -> None:
    deep: Any = "x"
    for _ in range(100_000):
        deep = [deep]
    with pytest.raises(RedactionFailed):
        redact_value(deep)


@pytest.mark.parametrize(
    "text",
    [
        "pan=5500-0000-0000-0004 0",
        "card 4111 1111 1111 1111 22 items",
        "4111111111111111 7",
    ],
)
def test_card_number_followed_by_digits_is_still_redacted(text: str) -> None:
    result = redact(text)
    assert "0000-0004" not in result.text and "1111 1111" not in result.text
    assert "4111111111111111" not in result.text
    assert result.counts == {"pan": 1}
