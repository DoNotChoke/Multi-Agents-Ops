"""Pure parts of the LAB=1 runner: alert matching against a scenario's expectations."""

from typing import Any

from evals.harness.lab import Want, missing, shop_alerts, wants


def _alert(name: str, state: str = "active", **labels: str) -> dict[str, Any]:
    return {
        "labels": {"alertname": name, "namespace": "shop", **labels},
        "status": {"state": state},
    }


def test_wants_come_from_the_scenario() -> None:
    scenario = {
        "expected_detection": {
            "alerts": [
                {"alertname": "HighLatencyP95", "service": "checkout"},
                {"alertname": "PodRestarting", "service": "payment"},
            ]
        }
    }
    assert wants(scenario) == (
        Want("HighLatencyP95", "checkout"),
        Want("PodRestarting", "payment"),
    )


def test_shop_alerts_keeps_only_active_shop_alerts() -> None:
    raw = [
        _alert("HighLatencyP95", service="checkout"),
        _alert("HighLatencyP95", state="suppressed", service="checkout"),
        {
            "labels": {"alertname": "Watchdog", "namespace": "monitoring"},
            "status": {"state": "active"},
        },
    ]
    assert shop_alerts(raw) == [raw[0]["labels"]]


def test_want_matches_service_label_or_pod_prefix() -> None:
    by_service = Want("HighLatencyP95", "checkout")
    by_pod = Want("ContainerOOMKilled", "payment")
    assert by_service.matches({"alertname": "HighLatencyP95", "service": "checkout"})
    assert not by_service.matches({"alertname": "HighLatencyP95", "service": "frontend"})
    assert not by_service.matches({"alertname": "HighErrorRate", "service": "checkout"})
    assert by_pod.matches({"alertname": "ContainerOOMKilled", "pod": "payment-7d9f-abcde"})
    assert not by_pod.matches({"alertname": "ContainerOOMKilled", "pod": "checkout-7d9f-abcde"})


def test_missing_lists_wants_not_yet_firing() -> None:
    expected = (Want("HighLatencyP95", "checkout"), Want("HighLatencyP95", "frontend"))
    firing = [{"alertname": "HighLatencyP95", "service": "checkout"}]
    assert missing(expected, firing) == [expected[1]]
    assert missing(expected, []) == list(expected)
