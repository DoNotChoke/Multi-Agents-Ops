"""Prometheus metrics about the platform itself (never per tenant: label cardinality)."""

from prometheus_client import Counter, Gauge

OUTBOX_LAG = Gauge("outbox_lag_seconds", "Age of the oldest unpublished outbox row")
OUTBOX_UNPUBLISHED = Gauge("outbox_unpublished_count", "Outbox rows not yet published")
INGEST_REQUESTS = Counter(
    "ingest_requests_total", "Webhook requests by outcome", ["source", "outcome"]
)
