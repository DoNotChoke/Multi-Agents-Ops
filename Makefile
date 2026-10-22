.PHONY: setup lint test

setup:
	uv sync
	go -C runner mod download
	pnpm -C web install --frozen-lockfile

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run lint-imports
	cd runner && golangci-lint run ./...
	pnpm -C web lint
	pnpm -C web typecheck

test:
	uv run pytest
	go -C runner test ./...
	pnpm -C web test
