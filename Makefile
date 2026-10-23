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
	@test -f deploy/compose/.env || cp deploy/compose/.env.example deploy/compose/.env
	$(COMPOSE) $(if $(LLM_OBS),--profile llm-obs) up -d --wait

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

# Runs every fault (or FAULT=<name>) and checks its alerts fire, then heals; needs `make lab-up`.
lab-verify:
	uv run python deploy/lab/verify_fault.py $(FAULT)

# Validates every scenario and writes evals/reports/<timestamp>/; needs no cluster.
eval:
	uv run python -m evals.run

test-security:
	uv run pytest tests/security

migrate test-integration: ; $(call not_implemented,T-1.1)
run-api:          ; $(call not_implemented,T-1.2)
run-ingest:       ; $(call not_implemented,T-2.1)
test-e2e:         ; $(call not_implemented,T-2.7)
run-worker:       ; $(call not_implemented,T-3.6)
run-runner:       ; $(call not_implemented,T-5.2)
