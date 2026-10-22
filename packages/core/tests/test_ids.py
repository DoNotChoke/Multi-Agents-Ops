import time
from typing import get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st
from opsteam_core.errors import InvalidId
from opsteam_core.ids import IdPrefix, new_id, parse_id

PREFIXES: tuple[IdPrefix, ...] = get_args(IdPrefix.__value__)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_new_id_round_trips(prefix: IdPrefix) -> None:
    value = new_id(prefix)
    assert value.startswith(f"{prefix}_")
    assert parse_id(value, prefix) == value


def test_ids_sort_by_creation_time() -> None:
    ids = []
    for _ in range(3):
        ids.append(new_id("inc"))
        time.sleep(0.002)
    assert ids == sorted(ids)


@pytest.mark.parametrize(
    "value",
    [
        "evt_01JBQ2Z8Y3K4M5N6P7Q8R9S0T1",  # wrong prefix
        "inc_01jbq2z8y3k4m5n6p7q8r9s0t1",  # lower case
        "inc_01JBQ2Z8Y3K4M5N6P7Q8R9S0T",  # too short
        "inc_01JBQ2Z8Y3K4M5N6P7Q8R9S0TI",  # I is not Crockford base32
        "inc01JBQ2Z8Y3K4M5N6P7Q8R9S0T1",
        "inc_01JBQ2Z8Y3K4M5N6P7Q8R9S0T1\n",
        "",
    ],
)
def test_parse_id_rejects_malformed(value: str) -> None:
    with pytest.raises(InvalidId):
        parse_id(value, "inc")


@given(st.text())
def test_parse_id_error_never_echoes_input(value: str) -> None:
    try:
        parse_id(value, "inc")
    except InvalidId as exc:
        assert str(exc) == "malformed inc id"
