"""contracts/: valid examples pass and invalid ones fail, in JSON Schema and in generated Pydantic.

Rules written as if/then/else (e.g. TaskEnvelope kind=action requires approval) exist only in
JSON Schema; the generated Pydantic models cover shape. Those examples are listed in SCHEMA_ONLY.
"""

import copy
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError
from referencing import Registry, Resource

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
SCHEMAS: dict[str, dict[str, Any]] = {
    p.stem: json.loads(p.read_text(encoding="utf-8"))
    for p in (CONTRACTS / "jsonschema").glob("*.json")
}
REGISTRY: Registry[Any] = Registry().with_resources(
    (s["$id"], Resource.from_contents(s)) for s in SCHEMAS.values()
)
EXAMPLES = sorted((CONTRACTS / "examples").glob("*/*.json"))
VALID = [p for p in EXAMPLES if p.name.startswith("valid")]
INVALID = [p for p in EXAMPLES if p.name.startswith("invalid")]
SCHEMA_ONLY = {
    "invalid-derived-without-sources.json",
    "invalid-action-without-approval.json",
    "invalid-query-with-approval.json",
}
MUTATIONS = [
    (path, mutation)
    for path in VALID
    for mutation in [
        "add:unexpected_field",
        *(f"drop:{f}" for f in SCHEMAS[path.parent.name]["required"]),
    ]
]


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        SCHEMAS[name], registry=REGISTRY, format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def _model(name: str) -> type[BaseModel]:
    model: type[BaseModel] = getattr(
        importlib.import_module(f"opsteam_contracts.{name}"), SCHEMAS[name]["title"]
    )
    return model


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _id(path: Path) -> str:
    return f"{path.parent.name}/{path.name}"


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_schema_is_valid_draft_2020_12(name: str) -> None:
    Draft202012Validator.check_schema(SCHEMAS[name])


def test_every_schema_has_a_valid_example() -> None:
    assert {p.parent.name for p in VALID} == set(SCHEMAS) - {"common"}


@pytest.mark.parametrize("path", VALID, ids=_id)
def test_valid_example_passes(path: Path) -> None:
    doc = _load(path)
    _validator(path.parent.name).validate(doc)
    _model(path.parent.name).model_validate(doc)


@pytest.mark.parametrize("path", INVALID, ids=_id)
def test_invalid_example_is_rejected(path: Path) -> None:
    doc = _load(path)
    assert not _validator(path.parent.name).is_valid(doc)
    if path.name not in SCHEMA_ONLY:
        with pytest.raises(ValidationError):
            _model(path.parent.name).model_validate(doc)


@pytest.mark.parametrize(("path", "mutation"), MUTATIONS, ids=lambda v: str(getattr(v, "name", v)))
def test_mutated_example_is_rejected(path: Path, mutation: str) -> None:
    doc = copy.deepcopy(_load(path))
    op, field = mutation.split(":")
    if op == "add":
        doc[field] = "x"
    else:
        del doc[field]
    assert not _validator(path.parent.name).is_valid(doc)
    with pytest.raises(ValidationError):
        _model(path.parent.name).model_validate(doc)
