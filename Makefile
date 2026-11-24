.PHONY: setup bootstrap up down migrate seed reset test-db test test-unit test-integration lint fmt typecheck chat eval-verify-cases eval eval-regrade eval-live

COMPANY ?= acme-001
ROLE ?= it_admin
MODEL ?= google_genai:gemini-3.8-flash

setup:
	uv sync --all-groups

bootstrap: up migrate seed test-db eval

up:
	docker compose up -d --wait

down:
	docker compose down

migrate:
	uv run python -m fleet_copilot.ops.migrate

seed:
	uv run python scripts/telemetry_sim.py seed

reset:
	uv run python scripts/telemetry_sim.py reset

test-db:
	uv run python -m fleet_copilot.ops.migrate --test-db

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

chat:
	uv run fleet-copilot chat --company $(COMPANY) --role $(ROLE)

eval-verify-cases:
	uv run pytest tests/eval -m eval

eval:
	LLM_REPLAY_MODE=replay uv run python -m evals.run --mode replay --tags core --repeats 1 --pass3-tags redteam

eval-regrade:
	LLM_REPLAY_MODE=replay uv run python -m evals.run --mode replay --tags core --repeats 1 --pass3-tags redteam --write-baseline

eval-live:
	LLM_REPLAY_MODE=record uv run python -m evals.run --mode live --model $(MODEL) --tags core --repeats 1 --pass3-tags redteam --max-cost 1.5 --write-baseline
