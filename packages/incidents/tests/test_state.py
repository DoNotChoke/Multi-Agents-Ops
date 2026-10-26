import pytest
from hypothesis import given
from hypothesis import strategies as st
from opsteam_incidents.state import (
    OPEN,
    STATES,
    InvalidTransition,
    check_transition,
)

# An independent copy of the lifecycle table, written from the spec rather than the code.
OPEN_STATES = {
    "NEW",
    "TRIAGED",
    "INVESTIGATING",
    "DIAGNOSED",
    "INCONCLUSIVE",
    "REMEDIATION_PROPOSED",
    "AWAITING_APPROVAL",
    "EXECUTING",
    "VERIFYING",
    "MANUAL_ACTION_REQUIRED",
}
ALLOWED = {
    ("NEW", "TRIAGED"),
    ("TRIAGED", "INVESTIGATING"),
    ("INVESTIGATING", "DIAGNOSED"),
    ("INVESTIGATING", "INCONCLUSIVE"),
    ("DIAGNOSED", "INVESTIGATING"),
    ("INCONCLUSIVE", "INVESTIGATING"),
    ("DIAGNOSED", "REMEDIATION_PROPOSED"),
    ("DIAGNOSED", "MANUAL_ACTION_REQUIRED"),
    ("REMEDIATION_PROPOSED", "AWAITING_APPROVAL"),
    ("AWAITING_APPROVAL", "EXECUTING"),
    ("AWAITING_APPROVAL", "MANUAL_ACTION_REQUIRED"),
    ("EXECUTING", "VERIFYING"),
    ("EXECUTING", "MANUAL_ACTION_REQUIRED"),
    ("VERIFYING", "RESOLVED"),
    ("VERIFYING", "MANUAL_ACTION_REQUIRED"),
    ("RESOLVED", "CLOSED"),
    ("RESOLVED", "INVESTIGATING"),
} | {(state, "RESOLVED") for state in OPEN_STATES}
ALL = sorted(OPEN_STATES | {"RESOLVED", "CLOSED"})


def test_states_match_the_spec() -> None:
    assert set(STATES) == set(ALL)
    assert set(OPEN) == OPEN_STATES


@pytest.mark.parametrize(("source", "target"), [(a, b) for a in ALL for b in ALL])
def test_every_pair(source: str, target: str) -> None:
    if (source, target) in ALLOWED:
        check_transition(source, target)
    else:
        with pytest.raises(InvalidTransition):
            check_transition(source, target)


def test_unknown_states_are_refused() -> None:
    with pytest.raises(InvalidTransition):
        check_transition("NEW", "DONE")
    with pytest.raises(InvalidTransition):
        check_transition("DONE", "RESOLVED")


@given(st.lists(st.sampled_from([*ALL, "DONE"]), max_size=40))
def test_no_walk_leaves_the_table(targets: list[str]) -> None:
    state = "NEW"
    for target in targets:
        try:
            check_transition(state, target)
        except InvalidTransition:
            continue
        assert (state, target) in ALLOWED
        state = target
    assert state in STATES
