"""Replay recording: fixed query templates, redaction before anything is written."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from evals.harness.replay import LOG_QUERIES, METRIC_QUERIES, record, write

SCENARIO = {"id": "checkout-db-pool-after-release", "setup": {"lab_fault": "bad_release_db_pool"}}
START = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
END = START + timedelta(minutes=5)


def _prom(query: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    return [{"metric": {"service": "checkout"}, "values": [[start.timestamp(), "0.5"]]}]


def _loki(query: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    line = '{"level":"ERROR","msg":"db login failed","error":"password=hunter2-dev-only"}'
    return [{"stream": {"service": "checkout"}, "values": [["1", line]]}]


def _events() -> list[dict[str, Any]]:
    return [{"reason": "ScalingReplicaSet", "object": "Deployment/checkout", "message": "scaled"}]


def _record() -> dict[str, Any]:
    return record(SCENARIO, START, END, prom=_prom, loki=_loki, events=_events)


def test_replay_has_schema_version_window_and_every_template() -> None:
    doc = _record()
    assert doc["schema_version"] == 1
    assert doc["scenario_id"] == SCENARIO["id"]
    assert doc["window"] == {"start_at": START.isoformat(), "end_at": END.isoformat()}
    assert {m["query_id"] for m in doc["metrics"]} == set(METRIC_QUERIES)
    assert {q["query_id"] for q in doc["logs"]} == set(LOG_QUERIES)
    assert doc["k8s_events"][0]["reason"] == "ScalingReplicaSet"


def test_replay_is_redacted_before_it_is_returned() -> None:
    text = json.dumps(_record())
    assert "hunter2-dev-only" not in text
    assert "[REDACTED" in text


def test_write_puts_replay_under_the_scenario_id(tmp_path: Path) -> None:
    path = write(_record(), tmp_path)
    assert path == tmp_path / SCENARIO["id"] / "replay.json"
    assert json.loads(path.read_text(encoding="utf-8"))["scenario_id"] == SCENARIO["id"]
