"""Redaction must remove every fake secret in the corpus and leave look-alike data intact."""

import json
from pathlib import Path
from typing import Any

import pytest
from opsteam_core.redact import redact

CORPUS = Path(__file__).parent / "redaction_corpus"


def _load(name: str) -> list[dict[str, Any]]:
    lines = (CORPUS / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


SECRETS = _load("secrets.jsonl")
BENIGN = _load("benign.jsonl")


@pytest.mark.parametrize("case", SECRETS, ids=lambda c: f"{c['kind']}:{c['secret'][:12]}")
def test_secret_is_removed(case: dict[str, Any]) -> None:
    result = redact(case["text"])
    assert case["secret"] not in result.text
    assert result.counts.get(case["kind"], 0) >= 1, result.counts


@pytest.mark.parametrize("case", BENIGN, ids=lambda c: c["text"][:30])
def test_benign_text_is_untouched(case: dict[str, Any]) -> None:
    result = redact(case["text"])
    assert result.text == case["text"]
    assert not result.counts


def test_every_kind_has_three_variants() -> None:
    kinds = [c["kind"] for c in SECRETS]
    assert all(kinds.count(k) >= 3 for k in set(kinds))
