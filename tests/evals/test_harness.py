"""Eval harness: scenario loading with precise errors, and the report it writes."""

import json
from pathlib import Path

import pytest
import yaml

from evals.harness.report import write_report
from evals.harness.scenario import SCENARIOS_DIR, ScenarioError, load_scenarios

VALID = {
    "id": "checkout-db-pool-after-release",
    "schema_version": 1,
    "description": "Release v2 shrinks the checkout DB pool.",
    "setup": {
        "lab_fault": "bad_release_db_pool",
        "warmup_seconds": 0,
        "fault_duration_seconds": 60,
    },
    "expected_detection": {
        "alerts": [{"alertname": "HighLatencyP95", "service": "checkout"}],
        "max_time_to_detect_seconds": 300,
    },
    "ground_truth": {
        "benign": False,
        "must_abstain": False,
        "root_cause": {
            "category": "deployment_regression",
            "entities": ["checkout"],
            "onset_at_offset_seconds": 0,
            "description": "pool of one",
        },
        "required_evidence_sources": ["metrics"],
    },
}


def _write(tmp_path: Path, doc: object, name: str = "checkout-db-pool-after-release") -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _errors(tmp_path: Path) -> list[str]:
    with pytest.raises(ScenarioError) as exc:
        load_scenarios(tmp_path)
    return exc.value.errors


def test_repo_scenarios_are_valid_and_cover_at_least_two_faults() -> None:
    scenarios = load_scenarios(SCENARIOS_DIR)
    assert len({s["setup"]["lab_fault"] for s in scenarios}) >= 2


def test_valid_scenario_loads(tmp_path: Path) -> None:
    _write(tmp_path, VALID)
    assert [s["id"] for s in load_scenarios(tmp_path)] == [VALID["id"]]


def test_missing_field_names_file_and_field(tmp_path: Path) -> None:
    doc = json.loads(json.dumps(VALID))
    del doc["setup"]["lab_fault"]
    _write(tmp_path, doc)
    [err] = _errors(tmp_path)
    assert "checkout-db-pool-after-release.yaml" in err
    assert "setup" in err and "'lab_fault' is a required property" in err


def test_bad_enum_reports_field_path(tmp_path: Path) -> None:
    doc = json.loads(json.dumps(VALID))
    doc["ground_truth"]["root_cause"]["category"] = "cosmic_rays"
    _write(tmp_path, doc)
    [err] = _errors(tmp_path)
    assert "ground_truth.root_cause.category" in err and "cosmic_rays" in err


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, {**VALID, "priority": "high"})
    [err] = _errors(tmp_path)
    assert "priority" in err


def test_id_must_match_file_name(tmp_path: Path) -> None:
    _write(tmp_path, VALID, name="something-else")
    [err] = _errors(tmp_path)
    assert "something-else.yaml" in err and "id" in err


def test_lab_fault_must_exist(tmp_path: Path) -> None:
    doc = json.loads(json.dumps(VALID))
    doc["setup"]["lab_fault"] = "solar_flare"
    _write(tmp_path, doc)
    [err] = _errors(tmp_path)
    assert "solar_flare" in err and "deploy/lab/faults" in err


def test_unparseable_yaml_is_reported(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("id: [unclosed", encoding="utf-8")
    [err] = _errors(tmp_path)
    assert "broken.yaml" in err


def test_every_broken_file_is_reported_at_once(tmp_path: Path) -> None:
    _write(tmp_path, {**VALID, "priority": "high"})
    (tmp_path / "broken.yaml").write_text("id: [unclosed", encoding="utf-8")
    assert len(_errors(tmp_path)) == 2


def test_report_has_git_sha_and_one_entry_per_scenario(tmp_path: Path) -> None:
    out = write_report([VALID], tmp_path)
    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert data["git_sha"] and data["scenario_count"] == 1
    assert data["results"][0]["id"] == VALID["id"]
    assert data["results"][0]["status"] == "skipped"
    assert VALID["id"] in (out / "report.md").read_text(encoding="utf-8")
