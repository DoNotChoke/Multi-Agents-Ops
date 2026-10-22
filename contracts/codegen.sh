#!/usr/bin/env bash
# Regenerates code from contracts/. Run via `make codegen`; never edit the output by hand.
set -euo pipefail
cd "$(dirname "$0")/.."

py_out=packages/generated/opsteam_contracts
rm -f "$py_out"/*.py
uv run datamodel-codegen \
  --input contracts/jsonschema \
  --input-file-type jsonschema \
  --output "$py_out" \
  --output-model-type pydantic_v2.BaseModel \
  --target-python-version 3.12 \
  --collapse-root-models \
  --use-type-alias \
  --use-annotated \
  --field-constraints \
  --use-standard-collections \
  --disable-timestamp \
  --formatters ruff-check ruff-format
uv run ruff check --fix --quiet "$py_out"
uv run ruff format --quiet "$py_out"

# common.json only holds $defs; each other schema inlines what it references.
ts_out=web/src/generated/contracts
rm -rf "$ts_out" && mkdir -p "$ts_out"
for schema in contracts/jsonschema/*.json; do
  name=$(basename "$schema" .json)
  [ "$name" = common ] && continue
  web/node_modules/.bin/json2ts --cwd contracts/jsonschema -i "$schema" -o "$ts_out/$name.ts"
done

# Go: the Runner only needs TaskEnvelope, kept by hand in runner/internal/contracts.
