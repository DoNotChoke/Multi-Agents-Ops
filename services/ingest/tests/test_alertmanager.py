from datetime import UTC, datetime
from typing import Any

import pytest
from opsteam_ingest.alertmanager import MAX_ALERTS, Payload, to_events
from pydantic import ValidationError

NOW = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)


def _alert(**over: Any) -> dict[str, Any]:
    return {
        "status": "firing",
        "labels": {"alertname": "HighLatencyP95", "service": "checkout", "severity": "page"},
        "annotations": {"summary": "checkout p95 above 500ms"},
        "startsAt": "2026-10-26T07:58:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://prometheus:9090/graph?g0.expr=x",
        "fingerprint": "abc",
    } | over


def _events(*alerts: dict[str, Any], collapse: bool = True) -> list[dict[str, Any]]:
    payload = Payload.model_validate({"version": "4", "status": "firing", "alerts": list(alerts)})
    events, _ = to_events(payload, "tnt_A", "int_A", collapse=collapse, received_at=NOW)
    return events


def test_maps_an_alertmanager_alert() -> None:
    [event] = _events(_alert())
    assert event["status"] == "firing"
    assert event["title"] == "checkout p95 above 500ms"
    assert event["severity_hint"] == "critical"
    assert event["starts_at"] == datetime(2026, 10, 26, 7, 58, tzinfo=UTC)
    assert event["ends_at"] is None  # Alertmanager's zero time
    assert event["received_at"] == NOW
    assert event["source_type"] == "alertmanager"
    assert event["annotations"]["generator_url"] == "http://prometheus:9090/graph?g0.expr=x"
    assert event["event_id"].startswith("evt_") and len(event["fingerprint"]) == 64


@pytest.mark.parametrize(
    ("severity", "hint"),
    [("page", "critical"), ("critical", "critical"), ("warning", "warning"), ("low", "info")],
)
def test_severity_hint(severity: str, hint: str) -> None:
    [event] = _events(_alert(labels={"alertname": "X", "severity": severity}))
    assert event["severity_hint"] == hint


def test_title_falls_back_to_alertname_and_is_capped() -> None:
    [plain] = _events(_alert(annotations={}))
    [long] = _events(_alert(annotations={"summary": "x" * 500}))
    assert plain["title"] == "HighLatencyP95"
    assert len(long["title"]) == 200


def test_only_http_generator_urls_are_kept() -> None:
    [bad] = _events(_alert(generatorURL="javascript:alert(1)"))
    [long] = _events(_alert(generatorURL="https://p/" + "a" * 600))
    assert "generator_url" not in bad["annotations"]
    assert len(long["annotations"]["generator_url"]) == 512


def test_labels_and_annotations_are_capped() -> None:
    labels = {f"k{i:02}": "v" * 300 for i in range(60)} | {"alertname": "X"}
    [event] = _events(_alert(labels=labels))
    assert len(event["labels"]) == 50
    assert all(len(v) <= 256 for v in event["labels"].values())
    assert event["labels"]["alertname"] == "X"  # identity labels survive the cut


def test_secrets_are_redacted_before_storage() -> None:
    token = "glpat-" + "a" * 20
    [event] = _events(
        _alert(
            labels={"alertname": "X", "url": f"https://u:{token}@h/x"},
            annotations={"summary": f"token {token} leaked", "password": "hunter2"},
        )
    )
    stored = str(event)
    assert token not in stored and "hunter2" not in stored
    assert "[REDACTED" in event["annotations"]["summary"]


def test_resolved_alert_keeps_its_end() -> None:
    [event] = _events(_alert(status="resolved", endsAt="2026-10-26T07:59:30Z"))
    assert event["status"] == "resolved"
    assert event["ends_at"] == datetime(2026, 10, 26, 7, 59, 30, tzinfo=UTC)


def test_fingerprint_ignores_replicas_when_collapsing() -> None:
    a = _alert(labels={"alertname": "X", "service": "checkout", "pod": "a"})
    b = _alert(labels={"alertname": "X", "service": "checkout", "pod": "b"})
    assert len({e["fingerprint"] for e in _events(a, b)}) == 1
    assert len({e["fingerprint"] for e in _events(a, b, collapse=False)}) == 2


def test_only_the_first_alerts_are_kept() -> None:
    payload = Payload.model_validate({"alerts": [_alert()] * (MAX_ALERTS + 1)})
    events, dropped = to_events(payload, "tnt_A", "int_A", collapse=True, received_at=NOW)
    assert (len(events), dropped) == (MAX_ALERTS, 1)


@pytest.mark.parametrize(
    "alert",
    [
        _alert(status="pending"),
        _alert(labels={"alertname": 1}),
        _alert(startsAt="yesterday"),
    ],
)
def test_malformed_alerts_are_rejected(alert: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Payload.model_validate({"alerts": [alert]})
