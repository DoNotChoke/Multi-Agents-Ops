import json
import random
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from opsteam_core.canonical import CanonicalizationError, canonical_json, canonicalize_text

VECTORS: list[dict[str, Any]] = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts/testvectors/canonical.json").read_text(
        encoding="utf-8"
    )
)
SAFE_INT = 2**53 - 1
# Integral floats >= 2**53 canonicalize to integer literals the parser then rejects; keep below.
json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-SAFE_INT, SAFE_INT)
    | st.floats(-SAFE_INT, SAFE_INT, allow_nan=False)
    | st.text(st.characters(exclude_categories=["Cs"])),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=20,
)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_shared_vector(vector: dict[str, Any]) -> None:
    if vector.get("error"):
        with pytest.raises(CanonicalizationError):
            canonicalize_text(vector["input"])
    else:
        assert canonicalize_text(vector["input"]) == vector["canonical"].encode("utf-8")


@given(json_values)
def test_canonical_form_is_a_fixed_point(value: Any) -> None:
    once = canonical_json(value)
    assert canonicalize_text(once) == once


@given(st.dictionaries(st.text(), st.integers(0, 9), min_size=2), st.randoms())
def test_key_insertion_order_does_not_matter(d: dict[str, int], rnd: random.Random) -> None:
    items = list(d.items())
    rnd.shuffle(items)
    assert canonical_json(dict(items)) == canonical_json(d)


def test_error_message_does_not_echo_the_value() -> None:
    with pytest.raises(CanonicalizationError) as exc:
        canonical_json([2**53])
    assert "9007199254740992" not in str(exc.value)
