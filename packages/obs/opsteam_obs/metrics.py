"""Prometheus metrics about the platform itself (never per tenant: label cardinality)."""

from prometheus_client import Counter, Gauge, Histogram

OUTBOX_LAG = Gauge("outbox_lag_seconds", "Age of the oldest unpublished outbox row")
OUTBOX_UNPUBLISHED = Gauge("outbox_unpublished_count", "Outbox rows not yet published")
INGEST_REQUESTS = Counter(
    "ingest_requests_total", "Webhook requests by outcome", ["source", "outcome"]
)
RECONCILIATION_GAP = Counter(
    "reconciliation_gap_total", "Alerts found by reading Alertmanager back that never arrived"
)
TIME_TO_DETECT = Histogram(
    "time_to_detect_seconds",
    "From an alert starting to its incident opening",
    buckets=(5, 15, 30, 60, 120, 300, 600, 1800),
)
TIME_TO_NOTIFY = Histogram(
    "time_to_notify_seconds",
    "From an incident opening to its first delivered notification",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 300),
)
