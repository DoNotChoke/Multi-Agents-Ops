"""Alertmanager webhook payloads (v4) and their mapping to normalized events."""

from datetime import datetime
from typing import Any, Literal

from opsteam_core.ids import new_id
from opsteam_core.redact import redact, redact_value
from opsteam_incidents.fingerprint import fingerprint
from pydantic import BaseModel, ConfigDict, Field

MAX_ALERTS = 200
MAX_LABELS = 50
MAX_LABEL_CHARS = 256
MAX_ANNOTATION_CHARS = 2048
MAX_URL_CHARS = 512
SEVERITY = {"page": "critical", "critical": "critical", "warning": "warning"}


class Alert(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime
    endsAt: datetime | None = None
    generatorURL: str = ""


class Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    alerts: list[Alert]


def _capped(values: dict[str, str], keys: int, chars: int) -> dict[str, str]:
    # alertname sorts first so the cut never drops the alert's identity.
    ordered = sorted(values.items(), key=lambda kv: (kv[0] != "alertname", kv[0]))
    return {k[:MAX_LABEL_CHARS]: v[:chars] for k, v in ordered[:keys]}


def _clean(values: dict[str, str]) -> dict[str, str]:
    redacted, _ = redact_value(dict(values))
    return {str(k): str(v) for k, v in redacted.items()}  # type: ignore[union-attr]


def to_events(
    payload: Payload,
    tenant_id: str,
    integration_id: str,
    *,
    collapse: bool,
    received_at: datetime,
) -> tuple[list[dict[str, Any]], int]:
    """Map alerts to event rows, redacted and capped; return them and how many were dropped."""
    events = [
        _event(alert, tenant_id, integration_id, collapse, received_at)
        for alert in payload.alerts[:MAX_ALERTS]
    ]
    return events, max(0, len(payload.alerts) - MAX_ALERTS)


def _event(
    alert: Alert, tenant_id: str, integration_id: str, collapse: bool, received_at: datetime
) -> dict[str, Any]:
    labels = _clean(_capped(alert.labels, MAX_LABELS, MAX_LABEL_CHARS))
    annotations = _clean(_capped(alert.annotations, MAX_LABELS, MAX_ANNOTATION_CHARS))
    url = alert.generatorURL
    if url.startswith(("http://", "https://")):
        annotations["generator_url"] = redact(url[:MAX_URL_CHARS]).text
    alertname = labels.get("alertname", "unnamed")
    return {
        "event_id": new_id("evt"),
        "tenant_id": tenant_id,
        "integration_id": integration_id,
        "source_type": "alertmanager",
        "fingerprint": fingerprint(
            tenant_id, integration_id, "alertmanager", alertname, labels, collapse=collapse
        ),
        "status": alert.status,
        "severity_hint": SEVERITY.get(labels.get("severity", ""), "info"),
        "title": (annotations.get("summary") or alertname)[:200],
        "labels": labels,
        "annotations": annotations,
        "starts_at": alert.startsAt,
        # Alertmanager sends the zero time for alerts that have not ended.
        "ends_at": alert.endsAt if alert.endsAt and alert.endsAt.year > 1 else None,
        "received_at": received_at,
    }
