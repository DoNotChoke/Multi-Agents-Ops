"""Record lab telemetry for a scenario window with fixed query templates, redacted before saving."""

import json
import shutil
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from opsteam_core.redact import redact_value

from evals.harness.scenario import REPO

REPLAY_DIR = REPO / "evals" / "replay"
PROM_URL = "http://127.0.0.1:30090/api/v1/query_range"
LOKI_URL = "http://127.0.0.1:30100/loki/api/v1/query_range"
STEP_S = 15
LOG_LIMIT = 200

METRIC_QUERIES = {
    "request_rate": 'sum by (service, code) (rate(http_requests_total{namespace="shop"}[1m]))',
    "latency_p95": (
        "histogram_quantile(0.95, sum by (service, le) "
        '(rate(http_request_duration_seconds_bucket{namespace="shop"}[1m])))'
    ),
    "db_pool_wait": 'sum by (pod) (rate(db_pool_acquire_wait_seconds_total{namespace="shop"}[1m]))',
    "restarts": 'sum by (pod) (kube_pod_container_status_restarts_total{namespace="shop"})',
    "memory_working_set": (
        'sum by (pod) (container_memory_working_set_bytes{namespace="shop", container="app"})'
    ),
}
LOG_QUERIES = {"errors": '{namespace="shop"} |= "\\"level\\":\\"ERROR\\""'}

type Series = list[dict[str, Any]]
type RangeQuery = Callable[[str, datetime, datetime], Series]


def _get(url: str, params: dict[str, str]) -> Any:
    query = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(query, timeout=30) as resp:  # noqa: S310 -- fixed localhost URLs
        return json.load(resp)["data"]["result"]


def _prom(query: str, start: datetime, end: datetime) -> Series:
    params = {"query": query, "start": str(start.timestamp()), "end": str(end.timestamp())}
    result: Series = _get(PROM_URL, {**params, "step": str(STEP_S)})
    return result


def _loki(query: str, start: datetime, end: datetime) -> Series:
    params = {"query": query, "limit": str(LOG_LIMIT), "direction": "backward"}
    span = {"start": str(int(start.timestamp() * 1e9)), "end": str(int(end.timestamp() * 1e9))}
    result: Series = _get(LOKI_URL, {**params, **span})
    return result


def _events() -> Series:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise RuntimeError("kubectl not found on PATH")
    out = subprocess.run(  # noqa: S603 -- fixed args
        [kubectl, "--context", "kind-opsteam-lab", "-n", "shop", "get", "events", "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        {
            "type": e.get("type"),
            "reason": e.get("reason"),
            "object": f"{e['involvedObject']['kind']}/{e['involvedObject']['name']}",
            "message": e.get("message"),
            "count": e.get("count"),
            "first_at": e.get("firstTimestamp") or e.get("eventTime"),
            "last_at": e.get("lastTimestamp") or e.get("eventTime"),
        }
        for e in json.loads(out)["items"]
    ]


def record(
    scenario: dict[str, Any],
    start: datetime,
    end: datetime,
    *,
    prom: RangeQuery = _prom,
    loki: RangeQuery = _loki,
    events: Callable[[], Series] = _events,
) -> dict[str, Any]:
    """Query every template over [start, end] and return the redacted replay document."""
    doc = {
        "schema_version": 1,
        "scenario_id": scenario["id"],
        "lab_fault": scenario["setup"]["lab_fault"],
        "window": {"start_at": start.isoformat(), "end_at": end.isoformat()},
        "step_seconds": STEP_S,
        "metrics": [
            {"query_id": qid, "promql": q, "result": prom(q, start, end)}
            for qid, q in METRIC_QUERIES.items()
        ],
        "logs": [
            {"query_id": qid, "logql": q, "limit": LOG_LIMIT, "result": loki(q, start, end)}
            for qid, q in LOG_QUERIES.items()
        ],
        "k8s_events": events(),
    }
    clean, _ = redact_value(json.loads(json.dumps(doc)))
    if not isinstance(clean, dict):
        raise TypeError("redaction changed the replay document's shape")
    return clean


def write(doc: dict[str, Any], root: Path = REPLAY_DIR) -> Path:
    """Save a replay document as <root>/<scenario_id>/replay.json and return the path."""
    path = root / doc["scenario_id"] / "replay.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=1, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")  # same bytes on every OS
    return path
