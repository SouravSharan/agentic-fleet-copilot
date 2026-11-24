# ADR 0001 — Tenant isolation in Postgres with a signed tenant lock

**Status:** accepted

## Context
The copilot answers questions for three companies from one database, and an LLM chooses which tools to call
with which arguments. A bug in a tool, a prompt-injection, or a hallucinated `company_id` must not be able to
read another company's data. Plain RLS keyed on a session variable (`SET app.current_tenant = …`) is not
enough: any role that can run SQL can also set that variable (we verified `SET LOCAL` bypassed it).

## Decision
- Two schemas (`telemetry`, `copilot`) and four least-privilege, `NOBYPASSRLS` login roles:
  `telemetry_writer` (ingest), `telemetry_ro` (reads + governed SQL), `copilot_app` (chat, proposals, audit),
  `demo_ops` (demo reset/scenarios through `SECURITY DEFINER` functions only). The owner DSN is used by
  migrations and the nightly job, never by the web app.
- Every transaction starts with `lock_tenant(company_id, issued_at, mac)`: the app signs
  `company_id|issued_at` with HMAC-SHA256 using `TENANT_LOCK_KEY`; the function (owner-only secret table)
  verifies the MAC, rejects timestamps older than 30 s, refuses a second lock in the same transaction and
  takes a transaction-scoped advisory lock as the marker.
- `current_tenant()` (`SECURITY DEFINER`) returns the company only when the signed lock is present; every RLS
  policy uses it, so a missing or forged lock returns **no rows** (fail closed).
- The pool resets session state (`RESET ALL`, `pg_advisory_unlock_all()`) when a connection is returned.

## Consequences
- Isolation holds even if the model, a tool, or the SQL validator is wrong; `make security-probe` runs 29
  live attacks (spoofed settings, `SET LOCAL`, `RESET`, forged/stale MACs, privilege escalation) on every run.
- Small cost: one extra function call per transaction (view latency regression measured < 20 %).
- The lock key is a real secret: hosted deployments must generate their own.
