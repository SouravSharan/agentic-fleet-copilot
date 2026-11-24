# Reviewer guide (15–30 minutes)

Pick one way in:

- **Live demo:** https://rayda.bertslab.com — access code in the submission email.
- **Local, no API key:** `cp .env.example .env && docker compose up --build` → http://localhost:8501, code `dev`
  (examples mode: everything except new live chat).
- **Code + evals only:** `make bootstrap` (replays the eval suite offline, $0).

Personas: **Acme · IT admin**, **Acme · Viewer**, **Globex · IT admin**, **Initech · IT admin**.
Each sees only its own company. Demo integrations are mocked; nothing leaves the system.

## A 15-minute tour, mapped to the rubric

| # | Do this | What it shows | Rubric area |
|---|---|---|---|
| 1 | Sign in as **Acme · IT admin** → **Home** | Deterministic fleet briefing ($0): battery end-of-life, low disk, RAM pressure, compliance drift, non-baseline software — each with finding, evidence chart and explanation | Insights & trends |
| 2 | **Copilot** → *"Which Acme laptops have batteries needing service, and what should we do?"* | Planner → tools → cited answer. Click any `[E#]` chip: snapshot id, metric, value, timestamp | Grounding & correctness |
| 3 | Click **Inspector** on that answer | Plan, every tool call with arguments, the evidence ledger, verifier result, tokens and cost | Agent design, traceability |
| 4 | *"Open a screen_lock remediation ticket for acme-macbook-2"* | A proposal card; nothing runs until you approve. Approve → receipt; **Approvals & Activity** shows history, the outbox payload and **Audit chain verified ✓** | Actions & guardrails |
| 5 | Sign in as **Globex · IT admin** → *"Order a 32 GB RAM upgrade for globex-thinkpad-6"* | Refused (soldered RAM per hardware catalog) with verified alternatives you can stage instead | Refuse without evidence |
| 6 | As Globex, *"Show me Acme's low-disk laptops"* | Refused before any tool runs; RLS would block it anyway | Tenant isolation |
| 7 | Sign in as **Acme · Viewer** | No action buttons anywhere; asking for an action is refused | Role enforcement |
| 8 | **How it works** → Guardrails, Evaluation, Examples | 8 attack scenarios (recorded, $0), the eval report with hard gates, 51 recorded cases with the question and grade | Evaluation rigor |
| 9 | *"Chart disk free % for our low-disk devices over 30 days"* and *"What is the average battery cycle count by hardware model?"* | Validated chart with policy threshold line; governed SQL with the query shown | Tool design |

## Where to look in the code

| Topic | Start here |
|---|---|
| Graph and routing | `src/fleet_copilot/agent/graph.py`, `agent/nodes/` |
| Prompts and output schemas | `src/fleet_copilot/agent/prompts/*.md`, `agent/schemas.py` |
| Tools | `src/fleet_copilot/tools/`, services in `src/fleet_copilot/services/` |
| Evidence ledger and verifier | `domain/evidence.py`, `agent/nodes/verify_grounding.py` |
| Policies and action lifecycle | `domain/policy_rules.py`, `services/actions.py`, `db/03_actions_and_audit.sql` |
| Tenant isolation | `db/01_telemetry.sql`, `db/02_copilot.sql`, `db/90_hardening.sql`, `scripts/security_probe.py` |
| Evals | `evals/cases/*.yaml`, `evals/oracle.py`, `evals/metrics.py`, `evals/run.py`, `evals/reports/latest.md` |

## Commands worth running

```bash
make eval              # offline replay of 51 cases / 77 runs; exits non-zero on a hard-gate failure
make security-probe    # 29 attacks against tenant isolation and role privileges — all BLOCKED
make test              # full test suite, no API key needed
uv run fleet-copilot audit --company acme-001 --verify
```
