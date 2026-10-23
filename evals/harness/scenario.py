"""Load eval scenarios from YAML and validate them against the EvalScenario contract."""

import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

REPO = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = REPO / "evals" / "scenarios"
FAULTS_DIR = REPO / "deploy" / "lab" / "faults"
SCHEMAS_DIR = REPO / "contracts" / "jsonschema"


class ScenarioError(Exception):
    """One or more scenario files are invalid; `errors` holds one line per problem."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


def _validator() -> Draft202012Validator:
    schemas = [json.loads(p.read_text(encoding="utf-8")) for p in SCHEMAS_DIR.glob("*.json")]
    registry: Registry[Any] = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in schemas
    )
    schema = next(s for s in schemas if s["title"] == "EvalScenario")
    return Draft202012Validator(schema, registry=registry)


def _where(error: ValidationError) -> str:
    return ".".join(str(p) for p in error.absolute_path) or "(root)"


def _display(path: Path) -> str:
    return path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path)


def _check(path: Path, validator: Draft202012Validator) -> tuple[dict[str, Any] | None, list[str]]:
    name = _display(path)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, [f"{name}: not valid YAML: {exc}"]
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    problems = [f"{name}: {_where(e)}: {e.message}" for e in errors]
    if problems:
        return None, problems
    if doc["id"] != path.stem:
        problems.append(f"{name}: id: {doc['id']!r} must match the file name")
    fault = doc["setup"]["lab_fault"]
    if not (FAULTS_DIR / fault).is_dir():
        problems.append(f"{name}: setup.lab_fault: {fault!r} is not a fault in deploy/lab/faults/")
    return (None if problems else doc), problems


def load_scenarios(directory: Path = SCENARIOS_DIR) -> list[dict[str, Any]]:
    """Return every scenario in `directory`, or raise ScenarioError listing all problems at once."""
    validator = _validator()
    scenarios: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        doc, problems = _check(path, validator)
        errors += problems
        if doc is not None:
            scenarios.append(doc)
    if errors:
        raise ScenarioError(errors)
    return scenarios
