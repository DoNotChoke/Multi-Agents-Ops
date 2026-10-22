.PHONY: setup lint test

setup:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run lint-imports

test:
	uv run pytest
