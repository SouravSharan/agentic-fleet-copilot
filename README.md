# Agentic Fleet Copilot

An agentic IT fleet copilot built with Python, PostgreSQL, and LangGraph that answers grounded telemetry questions, detects time-series fleet trends, and proposes human-approved operational actions under strict tenant isolation.

## Documentation

Detailed technical specs live in `docs/`:

- [01 · System Boundaries & Tenancy Model](docs/01-system-and-tenancy.md) — system boundary, actors/roles, tenant isolation invariants.
- [02 · Data Model & Persistence](docs/02-data-model.md) — `telemetry` (read-only + RLS + live JSONB views) and `copilot` (checkpoints, policies, HITL queue, append-only audit logs) schemas.
- `03 · Agent Architecture & Guardrails` *(Milestone 2–3)*
- `04 · Evaluation & Deployment` *(Milestone 4–5)*

---

## 1. Installation & Setup

### Prerequisites
- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)
- Docker & Docker Compose v2 (for local PostgreSQL 16)
- Telemetry dataset downloaded to `data/device-telemetry-dataset.ndjson` (not committed to git)

### Local Setup *(Stage 1 — Database & Environment)*
```bash
uv sync --all-groups
cp .env.example .env
docker compose up -d --wait
uv run python scripts/db_setup.py
uv run python scripts/telemetry_sim.py seed
```

---

## 2. Running the Agent

*(Added in Milestone 3–4: FastAPI service, Rich interactive CLI, and Web UI.)*

---

## 3. Running Evaluations

*(Stage 1 DB isolation & resilience tests are run via `uv run pytest tests/test_db_layer.py -v`. Full 2-tier agent evaluation suite added in Milestone 5.)*

---

## 4. Agent Architecture

See [docs/01-system-and-tenancy.md](docs/01-system-and-tenancy.md) for the outer system boundary and tenancy model. The LangGraph `StateGraph` orchestration design (Planner → bounded Investigator loop → Grounding Verifier & Action Policy Gate → `interrupt()` HITL → Freshness Check & Executor → Cited Synthesizer) is implemented in Milestone 3.

---

## 5. Tool Catalog

*(Populated in Milestone 2: Tier 0 read/analytics tools over `telemetry.*`, Tier 1 copilot policy/history tools, Tier 2 HITL-gated action tools: `create_upgrade_order`, `open_remediation_ticket`, `flag_device_for_replacement`, `notify_employee`.)*

---

## 6. Grounding Strategy

- **Deterministic queries over structured telemetry**: Telemetry queries and time-series trend calculations execute against `telemetry.*` SQL views rather than vector-embedded JSON snapshots, avoiding counting errors and threshold hallucinations.
- **Snapshot-level traceability**: Every snapshot has a stable `snapshot_id` (`telemetry.telemetry_snapshots`), and every read tool returns structured evidence anchors cited in answers and action proposals.
- **Zero-result honesty**: Queries with zero matches return explicit verified zero-match evidence rather than substituting lower-severity checks.

---

## 7. Guardrails

- **Database-enforced tenant isolation**: `telemetry_ro` has `SELECT`-only access with fail-closed PostgreSQL Row-Level Security (`app.current_tenant`); `copilot_app` cannot access `telemetry.*` and has `SELECT, INSERT`-only access on `copilot.audit_logs`.
- **Single-tenant runtime context**: `company_id` is injected from verified request auth (never an LLM tool argument).
- **Evidence-gated actions & HITL** *(Milestone 3)*: Deterministic policy refusal for unsupported actions, batched LangGraph `interrupt()` human approval, and execution-time freshness re-checks.

---

## 8. Key Design Decisions & Trade-offs

- **Two schemas + three strict Postgres roles (`telemetry_writer`, `telemetry_ro`, `copilot_app`)** instead of two physical databases during local/single-instance deploy: preserves strict DB-level access isolation while remaining compatible with single-DB managed Postgres (e.g. Supabase) and trivially splittable across separate hosts via env vars.
- **Raw `JSONB` + live `security_invoker` views** instead of rigid normalized ingestion tables: only the 5 guaranteed telemetry fields are typed columns, so live snapshots with missing optional blocks never break ingestion and are immediately queryable.
- **Deterministic SQL/analytics instead of vector RAG over telemetry**: time-series trends, exact counts, and semver comparisons require deterministic computation.
- **No cross-tenant platform meta-agent**: global defaults, tool kill-switches, and registry changes stay deterministic code/config rather than introducing a cross-tenant LLM context.
