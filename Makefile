.PHONY: setup codegen dev-up dev-down dev-reset dev-seed migrate lab-up lab-down lab-fault lab-heal lab-verify \
	lint test test-integration test-security test-e2e eval \
	run-api run-worker run-ingest run-web run-runner toolcall

REQUIRED_TOOLS := uv go pnpm golangci-lint

# Local opa CLI if installed, else the same image dev-up runs; MSYS_NO_PATHCONV keeps Git Bash
# from rewriting the container paths.
OPA_IMAGE := openpolicyagent/opa:1.21.3-debug
OPA := $(if $(shell command -v opa 2>/dev/null),opa,MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR)/policies:/policies:ro" -w / $(OPA_IMAGE))

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
	$(OPA) check --strict policies
	cd runner && golangci-lint run ./...
	cd deploy/lab/demo-app && golangci-lint run ./...
	pnpm -C web lint
	pnpm -C web format:check
	pnpm -C web typecheck

test:
	uv run pytest
	$(OPA) test policies
	go -C runner test ./...
	go -C deploy/lab/demo-app test ./...
	pnpm -C web test

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

# Fake shared secret: dev-seed stores it, the lab Alertmanager sends it to run-ingest.
LAB_WEBHOOK_SECRET ?= dev-only-lab-webhook-secret

lab-up:
	LAB_WEBHOOK_SECRET=$(LAB_WEBHOOK_SECRET) bash deploy/lab/lab.sh up

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
test-integration test-security: export OPSTEAM_SYSTEM_DB_PASSWORD := $(OPSTEAM_SYSTEM_DB_PASSWORD)
migrate: export OPSTEAM_MIGRATOR_DATABASE_URL ?= $(call DB_URL,opsteam_migrator,$(OPSTEAM_MIGRATOR_DB_PASSWORD))

migrate:
	uv run alembic -c packages/db/alembic.ini upgrade head

# Needs `make dev-up`; each run migrates a throwaway database.
test-integration:
	uv run pytest -m integration

# Includes the DB-backed cross-tenant tests, so it needs `make dev-up`.
test-security:
	uv run pytest tests/security -m ""

# Fake dev settings for the local API process; the token issuer is the compose Keycloak.
run-api dev-seed: export OPSTEAM_ENV ?= dev
run-api dev-seed: export OPSTEAM_DATABASE_URL ?= $(call DB_URL,opsteam_app,$(OPSTEAM_APP_DB_PASSWORD))
run-api dev-seed: export OPSTEAM_OIDC_ISSUER ?= http://localhost:8080/realms/opsteam
run-api dev-seed: export OPSTEAM_OIDC_JWKS_URL ?= http://localhost:8080/realms/opsteam/protocol/openid-connect/certs
# Fake 32-byte key built at run time; the lab Prometheus is the only SSRF exception.
run-api dev-seed: export OPSTEAM_SECRETS_KEY ?= $(shell printf 'dev-only-secrets-key-32-bytes!!!' | base64)
run-api dev-seed: export OPSTEAM_NETGUARD_DEV_ALLOW ?= ["127.0.0.1:30090"]
run-api:
	uv run uvicorn --factory opsteam_api.main:create_app --host 127.0.0.1 --port 8000

# One metrics.query_range through the full Gateway (dev only), e.g.
# make toolcall ARGS="--tenant tnt_… --integration int_… --service checkout --environment shop"
toolcall: export OPSTEAM_ENV ?= dev
toolcall: export OPSTEAM_DATABASE_URL ?= $(call DB_URL,opsteam_app,$(OPSTEAM_APP_DB_PASSWORD))
toolcall: export OPSTEAM_SECRETS_KEY ?= $(shell printf 'dev-only-secrets-key-32-bytes!!!' | base64)
toolcall: export OPSTEAM_NETGUARD_DEV_ALLOW ?= ["127.0.0.1:30090"]
toolcall: export OPSTEAM_REDIS_URL ?= redis://:$(REDIS_PASSWORD)@127.0.0.1:6379/0
toolcall: export OPSTEAM_S3_ACCESS_KEY ?= $(S3_ACCESS_KEY)
toolcall: export OPSTEAM_S3_SECRET_KEY ?= $(S3_SECRET_KEY)
toolcall:
	uv run python -m opsteam_worker.toolcall $(ARGS)

# Browser test of the web app; needs dev-up, lab-up, dev-seed and run-api.
test-e2e: export KEYCLOAK_DEV_USER_PASSWORD ?= dev-only-user-password
test-e2e:
	pnpm -C web exec playwright test

# Dev settings (compose Keycloak, run-api) come from web/.env.development.
run-web:
	pnpm -C web dev

# Dev tenant acme-dev with the four realm users; needs `make dev-up migrate`.
dev-seed: export OPSTEAM_LAB_WEBHOOK_SECRET ?= $(LAB_WEBHOOK_SECRET)
dev-seed:
	uv run python -m opsteam_api.devseed

run-ingest: export OPSTEAM_ENV ?= dev
run-ingest: export OPSTEAM_DATABASE_URL ?= $(call DB_URL,opsteam_app,$(OPSTEAM_APP_DB_PASSWORD))
run-ingest: export OPSTEAM_SYSTEM_DATABASE_URL ?= $(call DB_URL,opsteam_system,$(OPSTEAM_SYSTEM_DB_PASSWORD))
run-ingest: export OPSTEAM_SECRETS_KEY ?= $(shell printf 'dev-only-secrets-key-32-bytes!!!' | base64)
run-ingest: export OPSTEAM_REDIS_URL ?= redis://:$(REDIS_PASSWORD)@127.0.0.1:6379/0
# Short so the lab shows auto-resolve within minutes of `make lab-heal`.
run-ingest: export OPSTEAM_INCIDENT_STABLE_WINDOW_SECONDS ?= 120
run-ingest:
	uv run uvicorn --factory opsteam_ingest.app:create_app --host 127.0.0.1 --port 8001
run-worker:       ; $(call not_implemented,T-3.6)
run-runner:       ; $(call not_implemented,T-5.2)
