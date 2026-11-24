.PHONY: setup env bootstrap up down migrate seed reset test-db test test-unit test-integration lint fmt typecheck chat api ui security-probe eval-verify-cases eval eval-regrade eval-live

COMPANY ?= acme-001
ROLE ?= it_admin
MODEL ?= google_genai:gemini-3.8-flash

setup:
	uv sync --all-groups

bootstrap: env up migrate seed test-db eval

# Create .env from the dev template if it doesn't exist yet (never overwrites).
env:
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example (dev values)")

# Postgres only, for the uv-based workflow. `docker compose up --build` runs the full stack.
up:
	docker compose up -d --wait postgres

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

api:
	uv run uvicorn fleet_copilot.interfaces.api.app:create_app --factory --host 127.0.0.1 --port 8000

ui:
	uv run streamlit run src/fleet_copilot/interfaces/ui/app.py

security-probe:
	uv run python scripts/security_probe.py

eval-verify-cases:
	uv run pytest tests/eval -m eval

eval:
	LLM_REPLAY_MODE=replay uv run python -m evals.run --mode replay --tags core --repeats 1 --pass3-tags redteam

eval-regrade:
	LLM_REPLAY_MODE=replay uv run python -m evals.run --mode replay --tags core --repeats 1 --pass3-tags redteam --write-baseline

eval-live:
	LLM_REPLAY_MODE=record uv run python -m evals.run --mode live --model $(MODEL) --tags core --repeats 1 --pass3-tags redteam --max-cost 1.5 --write-baseline
