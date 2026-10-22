.PHONY: setup lint test

setup:
	uv sync
	go -C runner mod download

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run lint-imports
	cd runner && golangci-lint run ./...

test:
	uv run pytest
	go -C runner test ./...
