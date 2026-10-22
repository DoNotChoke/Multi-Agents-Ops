.PHONY: setup codegen dev-up dev-down migrate lab-up lab-down lab-fault lab-heal \
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
	pnpm -C web install --frozen-lockfile
	uv run pre-commit install

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run lint-imports
	cd runner && golangci-lint run ./...
	pnpm -C web lint
	pnpm -C web format:check
	pnpm -C web typecheck

test:
	uv run pytest
	go -C runner test ./...
	pnpm -C web test

run-web:
	pnpm -C web dev

codegen:          ; $(call not_implemented,T-0.2)
dev-up dev-down:  ; $(call not_implemented,T-0.3)
lab-up lab-down lab-fault lab-heal: ; $(call not_implemented,T-0.4)
eval:             ; $(call not_implemented,T-0.5)
test-security:    ; $(call not_implemented,T-0.7)
migrate test-integration: ; $(call not_implemented,T-1.1)
run-api:          ; $(call not_implemented,T-1.2)
run-ingest:       ; $(call not_implemented,T-2.1)
test-e2e:         ; $(call not_implemented,T-2.7)
run-worker:       ; $(call not_implemented,T-3.6)
run-runner:       ; $(call not_implemented,T-5.2)
