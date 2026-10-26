from hypothesis import assume, given
from hypothesis import strategies as st
from opsteam_incidents.fingerprint import REPLICA_LABELS, fingerprint

BASE = ("tnt_A", "int_A", "alertmanager", "HighLatencyP95")


def test_label_order_does_not_matter() -> None:
    a = fingerprint(*BASE, {"service": "checkout", "namespace": "shop"}, collapse=True)
    b = fingerprint(*BASE, {"namespace": "shop", "service": "checkout"}, collapse=True)
    assert a == b and len(a) == 64


def test_replica_labels_collapse_only_when_asked() -> None:
    one = {"service": "checkout", "pod": "checkout-7f9-abc", "instance": "10.0.0.1:8080"}
    two = {"service": "checkout", "pod": "checkout-7f9-xyz", "instance": "10.0.0.2:8080"}
    assert fingerprint(*BASE, one, collapse=True) == fingerprint(*BASE, two, collapse=True)
    assert fingerprint(*BASE, one, collapse=False) != fingerprint(*BASE, two, collapse=False)


def test_identity_fields_and_meaningful_labels_change_it() -> None:
    labels = {"service": "checkout"}
    seen = {
        fingerprint(*BASE, labels, collapse=True),
        fingerprint("tnt_B", *BASE[1:], labels, collapse=True),
        fingerprint(BASE[0], "int_B", *BASE[2:], labels, collapse=True),
        fingerprint(*BASE[:3], "HighErrorRate", labels, collapse=True),
        fingerprint(*BASE, {"service": "payment"}, collapse=True),
    }
    assert len(seen) == 5


def test_separator_cannot_be_forged_across_fields() -> None:
    assert fingerprint("tnt_A", "int_A|x", "alertmanager", "y", {}, collapse=True) != fingerprint(
        "tnt_A", "int_A", "x|alertmanager", "y", {}, collapse=True
    )


labels_st = st.dictionaries(st.text(min_size=1, max_size=20), st.text(max_size=40), max_size=12)


@given(labels_st)
def test_order_never_matters(labels: dict[str, str]) -> None:
    shuffled = dict(reversed(list(labels.items())))
    assert fingerprint(*BASE, labels, collapse=True) == fingerprint(*BASE, shuffled, collapse=True)


@given(labels_st, st.sampled_from(sorted(REPLICA_LABELS)), st.text(max_size=40))
def test_replica_noise_never_matters_when_collapsing(
    labels: dict[str, str], noisy: str, value: str
) -> None:
    assert fingerprint(*BASE, labels | {noisy: value}, collapse=True) == fingerprint(
        *BASE, {k: v for k, v in labels.items() if k != noisy}, collapse=True
    )


@given(labels_st, st.text(min_size=1, max_size=20), st.text(max_size=40))
def test_a_changed_meaningful_label_changes_it(
    labels: dict[str, str], key: str, value: str
) -> None:
    assume(key not in REPLICA_LABELS and labels.get(key) != value)
    assert fingerprint(*BASE, labels, collapse=True) != fingerprint(
        *BASE, labels | {key: value}, collapse=True
    )
