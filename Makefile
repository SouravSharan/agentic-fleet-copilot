.PHONY: setup up down migrate seed reset test-db test test-unit test-integration lint fmt typecheck

setup:
	uv sync --all-groups

up:
	docker compose up -d --wait

down:
	docker compose down

migrate:
	uv run python scripts/db_setup.py

seed:
	uv run python scripts/telemetry_sim.py seed

reset:
	uv run python scripts/telemetry_sim.py reset

test-db:
	uv run python scripts/db_setup.py --test-db

test:
	uv run pytest -m "not live"

test-unit:
	uv run pytest -m unit

test-integration:
	uv run pytest -m integration

lint:
	uv run ruff check src scripts tests evals && uv run ruff format --check src scripts tests evals

fmt:
	uv run ruff format src scripts tests evals && uv run ruff check --fix src scripts tests evals

typecheck:
	uv run pyright
