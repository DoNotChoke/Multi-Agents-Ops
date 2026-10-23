.PHONY: setup codegen dev-up dev-down dev-reset migrate lab-up lab-down lab-fault lab-heal lab-verify \
	lint test test-integration test-security test-e2e eval \
	run-api run-worker run-ingest run-web run-runner

REQUIRED_TOOLS := uv go pnpm golangci-lint

# Fails loudly so a stub can never pass for a real check (e.g. in CI).
not_implemented = @echo "make $@: not implemented yet ($(1))" >&2; exit 1

setup:
	@missing=""; for t in $(REQUIRED_TOOLS); do command -v $$t >/dev/null 2>&1 || missing="$$missing $$t"; done; \
	if [ -n "$$missing" ]; then echo "missing tools:$$missing" >&2; exit 1; fi
	uv sync
	go -C runner mod download
	go -C deploy/lab/demo-app mod download
	pnpm -C web install --frozen-lockfile
	uv run pre-commit install

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run lint-imports
	cd runner && golangci-lint run ./...
	cd deploy/lab/demo-app && golangci-lint run ./...
	pnpm -C web lint
	pnpm -C web format:check
	pnpm -C web typecheck

test:
	uv run pytest
	go -C runner test ./...
	go -C deploy/lab/demo-app test ./...
	pnpm -C web test

run-web:
	pnpm -C web dev

codegen:
	bash contracts/codegen.sh

COMPOSE := docker compose -f deploy/compose/docker-compose.yml

# LLM_OBS=1 also starts Langfuse and ClickHouse.
dev-up:
	@# Adds keys new in .env.example without touching values already in .env.
	@touch deploy/compose/.env; grep -o '^[A-Z0-9_]*=' deploy/compose/.env.example | while read k; do \
		grep -q "^$$k" deploy/compose/.env || grep "^$$k" deploy/compose/.env.example >> deploy/compose/.env; done
	$(COMPOSE) $(if $(LLM_OBS),--profile llm-obs) up -d --wait
	@# Roles are also created by the init script, but only on an empty volume.
	$(COMPOSE) exec -T postgres sh /docker-entrypoint-initdb.d/02-roles.sh

dev-down:
	$(COMPOSE) --profile llm-obs down

# Also deletes the volumes (all local data).
dev-reset:
	$(COMPOSE) --profile llm-obs down -v
lab-up:
	bash deploy/lab/lab.sh up

lab-down:
	bash deploy/lab/lab.sh down

lab-fault:
	bash deploy/lab/lab.sh fault $(FAULT)

lab-heal:
	bash deploy/lab/lab.sh heal

# Same as `make eval LAB=1`: every scenario's fault on the lab, alerts timed, then healed.
lab-verify:
	uv run python -m evals.run --lab $(SCENARIO)

# Validates scenarios and writes evals/reports/<timestamp>/ without a cluster.
# LAB=1 applies each fault on the kind lab; RECORD=1 also saves evals/replay/<id>/; SCENARIO=<ids>.
eval:
	uv run python -m evals.run $(if $(LAB),--lab) $(if $(RECORD),--record) $(SCENARIO)

# Fake dev credentials come from the compose env file; CI sets the same variables directly.
-include deploy/compose/.env
DB_URL = postgresql+asyncpg://$(1):$(2)@127.0.0.1:5432/opsteam
migrate test-integration test-security: export OPSTEAM_APP_DB_PASSWORD := $(OPSTEAM_APP_DB_PASSWORD)
migrate test-integration test-security: export OPSTEAM_MIGRATOR_DB_PASSWORD := $(OPSTEAM_MIGRATOR_DB_PASSWORD)
migrate: export OPSTEAM_MIGRATOR_DATABASE_URL ?= $(call DB_URL,opsteam_migrator,$(OPSTEAM_MIGRATOR_DB_PASSWORD))

migrate:
	uv run alembic -c packages/db/alembic.ini upgrade head

# Needs `make dev-up`; each run migrates a throwaway database.
test-integration:
	uv run pytest -m integration

# Includes the DB-backed cross-tenant tests, so it needs `make dev-up`.
test-security:
	uv run pytest tests/security -m ""

run-api:          ; $(call not_implemented,T-1.2)
run-ingest:       ; $(call not_implemented,T-2.1)
test-e2e:         ; $(call not_implemented,T-2.7)
run-worker:       ; $(call not_implemented,T-3.6)
run-runner:       ; $(call not_implemented,T-5.2)
