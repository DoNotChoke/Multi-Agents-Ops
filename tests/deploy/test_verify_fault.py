"""Unit tests for the pure parts of the lab fault verifier."""

import importlib.util
from pathlib import Path

LAB = Path(__file__).resolve().parents[2] / "deploy" / "lab"
_spec = importlib.util.spec_from_file_location("verify_fault", LAB / "verify_fault.py")
assert _spec and _spec.loader
vf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vf)


def _alert(name: str, state: str = "active", **labels: str) -> dict[str, object]:
    return {
        "labels": {"alertname": name, "namespace": "shop", **labels},
        "status": {"state": state},
    }


def test_every_fault_directory_has_an_expectation() -> None:
    dirs = {p.name for p in (LAB / "faults").iterdir() if p.is_dir()}
    assert dirs == set(vf.FAULTS)


def test_benign_noise_expects_no_alert_and_others_expect_some() -> None:
    assert vf.FAULTS["benign_noise"].expect == ()
    assert all(f.expect for name, f in vf.FAULTS.items() if name != "benign_noise")


def test_shop_alerts_keeps_only_active_shop_alerts() -> None:
    raw = [
        _alert("HighLatencyP95", service="checkout"),
        _alert("HighLatencyP95", state="suppressed", service="checkout"),
        {
            "labels": {"alertname": "Watchdog", "namespace": "monitoring"},
            "status": {"state": "active"},
        },
    ]
    assert vf.shop_alerts(raw) == [raw[0]["labels"]]


def test_want_matches_service_label_or_pod_prefix() -> None:
    by_service = vf.Want("HighLatencyP95", "checkout")
    by_pod = vf.Want("ContainerOOMKilled", "payment")
    assert by_service.matches({"alertname": "HighLatencyP95", "service": "checkout"})
    assert not by_service.matches({"alertname": "HighLatencyP95", "service": "frontend"})
    assert not by_service.matches({"alertname": "HighErrorRate", "service": "checkout"})
    assert by_pod.matches({"alertname": "ContainerOOMKilled", "pod": "payment-7d9f-abcde"})
    assert not by_pod.matches({"alertname": "ContainerOOMKilled", "pod": "checkout-7d9f-abcde"})


def test_missing_lists_wants_not_yet_firing() -> None:
    wants = (vf.Want("HighLatencyP95", "checkout"), vf.Want("HighLatencyP95", "frontend"))
    firing = [{"alertname": "HighLatencyP95", "service": "checkout"}]
    assert vf.missing(wants, firing) == [wants[1]]
    assert vf.missing(wants, []) == list(wants)
