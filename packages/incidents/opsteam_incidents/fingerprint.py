"""Event fingerprints: the identity of an alert, so retries and replicas dedup to one event."""

import hashlib

from opsteam_core.canonical import JsonValue, canonical_json

# Labels that differ per replica of the same workload; dropped when collapsing.
REPLICA_LABELS = frozenset(
    {
        "pod",
        "pod_name",
        "instance",
        "container_id",
        "uid",
        "pod_template_hash",
        "controller_revision_hash",
        "endpoint",
    }
)


def fingerprint(
    tenant_id: str,
    integration_id: str,
    source_type: str,
    alertname: str,
    labels: dict[str, str],
    *,
    collapse: bool,
) -> str:
    """Return the sha256 hex identity of an alert; label order never matters."""
    kept: dict[str, JsonValue] = {
        k: v for k, v in labels.items() if not (collapse and k in REPLICA_LABELS)
    }
    # A JSON array rather than a joined string, so no field can smuggle a separator.
    identity = canonical_json([tenant_id, integration_id, source_type, alertname, kept])
    return hashlib.sha256(identity).hexdigest()
