# 02 · Data Model & Persistence

## PostgreSQL Isolation Layout

Both data boundaries run on PostgreSQL 16 as two isolated schemas (`telemetry` and `copilot`) accessed by four `NOBYPASSRLS` login roles. The application connects using separate connection URLs (`TELEMETRY_RO_DATABASE_URL`, `COPILOT_DATABASE_URL`, `DEMO_OPS_DATABASE_URL`) and schema-qualified queries, so the same code works on:
- one local Docker Postgres container,
- one managed Postgres project (e.g. Supabase), or
- two physically separate Postgres servers.

| Role | Schema Access | Permissions | Used By |
|---|---|---|---|
| `telemetry_writer` | `telemetry` only | `SELECT, INSERT, UPDATE, TRUNCATE` on raw tables | External simulator (`scripts/telemetry_sim.py`) only — never available to the Copilot app |
| `telemetry_ro` | `telemetry` only | `SELECT` only (`default_transaction_read_only = on`) + fail-closed RLS on `company_id` | Fleet Copilot read/analytics tools |
| `copilot_app` | `copilot` only (`search_path = copilot`) | `SELECT, INSERT, UPDATE` on `threads`, `turns`, `action_proposals`; `SELECT, INSERT, UPDATE, DELETE` on `tenant_policies`, `tenant_tool_configs`; **`SELECT, INSERT` only** on `audit_logs`; `CREATE` on `copilot` for LangGraph `PostgresSaver.setup()` | Fleet Copilot service (UI, CLI, API) |
| `demo_ops` | no direct table access | `EXECUTE` on `ops.demo_reset_company()` and `ops.inject_scenario()` (`SECURITY DEFINER`, gated by `ops.runtime_settings.demo_mode`) | UI demo controls only — lets the web app reset one tenant's demo without holding the owner DSN |

All Python connections use thread-safe `psycopg_pool.ConnectionPool` (`prepare_threshold=None`, `check=ConnectionPool.check_connection`) and bind `app.current_tenant` transaction-locally through write-once `lock_tenant()` functions so tenant context never leaks across pooled connections.

### Tenant Lock

Both schemas expose a credentialed `SECURITY DEFINER` function (`telemetry.lock_tenant(text, timestamptz, text)` for `telemetry_ro`, `copilot.lock_tenant(text, timestamptz, text)` for `copilot_app`) with `SET search_path = pg_catalog, <schema>`. Each function:
1. Validates `company_id` against the strict identifier regex (rejecting empty values and `*`).
2. Refuses to re-lock if this backend already holds a transaction-level advisory lock (`K = 73110` telemetry / `73120` copilot) — a marker that `RESET` cannot clear.
3. Verifies a 30 s freshness window and an HMAC-SHA256 MAC (`TENANT_LOCK_KEY`, synced by `ops.migrate` into owner-only `*.tenant_lock_secret` tables via `pgcrypto`). `DatabaseManager` computes the MAC using bound parameters (never interpolated, never logged).
4. Acquires `pg_advisory_xact_lock_shared(<K>, hashtext(company_id))` and writes `app.current_tenant`, `app.tenant_sig`, and `app.tenant_locked` transaction-locally.

Every RLS policy filters on `(SELECT <schema>.current_tenant())`, a `SECURITY DEFINER STABLE` verifier that returns the tenant only when both the transaction-scoped SHA-256 signature and the matching advisory lock are present (else `NULL`, fail closed). Pooled connections run `RESET ALL` and `pg_advisory_unlock_all()` on return, and `db/90_hardening.sql` revokes `EXECUTE` on `pg_catalog.set_config(text, text, boolean)` from all application roles. Even arbitrary SQL running as an app role cannot read another tenant without the application-held key (`TENANT_LOCK_KEY`).

---

## 1. `telemetry` Schema (External, Read-Only to Copilot)

### Raw Tables

Only the five fields guaranteed present in every snapshot are typed columns; all optional blocks stay in `payload JSONB` (`CHECK (jsonb_typeof(payload) = 'object')`) so partial payloads and schema drift across `agent_version`s never fail ingestion.

- **`telemetry.companies`**: `company_id TEXT PK CHECK (company_id <> '*')`, `display_name TEXT NOT NULL`, `created_at TIMESTAMPTZ` (`acme-001 → Acme`, `globex-002 → Globex`, `initech-003 → Initech`).
- **`telemetry.telemetry_snapshots`**:
  - `id BIGSERIAL PK`
  - `snapshot_id TEXT UNIQUE NOT NULL` — stable citation anchor (`snap_<16-hex>`)
  - `company_id TEXT NOT NULL REFERENCES telemetry.companies(company_id)`
  - `device_id TEXT NOT NULL`
  - `employee_id TEXT NOT NULL`
  - `collected_at TIMESTAMPTZ NOT NULL` — device event time
  - `agent_version TEXT NOT NULL`
  - `ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()` — platform arrival time
  - `payload JSONB NOT NULL` — full raw JSON snapshot
  - Indexes: `(company_id, device_id, collected_at DESC, ingested_at DESC, id DESC)`, `(company_id, collected_at DESC)`, `(company_id, employee_id)`.

### Fail-Closed Row-Level Security

`ENABLE ROW LEVEL SECURITY` is enabled on `telemetry.companies` and `telemetry.telemetry_snapshots` (enforced on all non-owner `NOBYPASSRLS` roles). Policy for `telemetry_ro`:

```sql
company_id = (SELECT telemetry.current_tenant())
```

`current_tenant()` returns `NULL` unless this transaction holds a valid signed tenant lock, so a missing or forged lock returns `0` rows.

### Live Read Views (`security_invoker = true, security_barrier = true`)

Because these are live views over `payload JSONB` using `IMMUTABLE` safe-cast helpers (`safe_jsonb_text`, `safe_jsonb_bigint`, `safe_jsonb_int`, `safe_jsonb_bool`) and `CASE WHEN jsonb_typeof(...) = 'array'` guards, inserting a single new (or drifted/malformed) JSON snapshot updates every view immediately with zero ETL lag while inheriting `telemetry_ro`'s RLS policy:

| View | Purpose | Edge Cases Handled |
|---|---|---|
| `telemetry.v_snapshots_deduped` | Deduplicated snapshot stream with safe nullable scalar extraction (`os_*`, `model_name`, `ram_*`, `battery_*`) | Deduplicates agent retries on `(company_id, device_id, collected_at)` keeping latest `(ingested_at, id)`; tolerates missing blocks, JSON `null`, floats in int fields, wrong JSON types, and non-positive denominators (`> 0`). |
| `telemetry.v_latest_device_state` | Current state per `(company_id, device_id)` + `first_seen_at`, `last_seen_at`, `snapshot_count`, `ever_had_battery`, `form_factor` | Orders by `collected_at DESC` (not `ingested_at`) so late snapshots never overwrite newer state. Carries forward last known hardware/OS identity across partial snapshots. Checks `ever_had_battery` before desktop model patterns (`laptop` / `desktop` / `unknown`). |
| `telemetry.v_disk_volumes` | Flattened `disk_volumes[]` with computed `free_pct` | `CASE WHEN jsonb_typeof(...) = 'array'` + object element filter + `size_bytes > 0` guard. |
| `telemetry.v_compliance_results` | Flattened `compliance_results[]` (`check_id`, `status`, `severity`) | Preserves exact upstream severity and pass/fail status from the desktop agent. |
| `telemetry.v_installed_software` | Flattened `installed_software[]` (`software_name`, `version`, `publisher`) | Handles missing or JSON-`null` `installed_software` cleanly. |
| `telemetry.v_network_interfaces` | Flattened `network[]` (`interface_name`, `address`, `family`, `mac`, `internal`) | Handles missing `network` blocks (observed in 16/750 dataset rows). |

### Telemetry Classification vs. Configurable Thresholds

- **Provided in telemetry (source of truth)**: `compliance_results` (`status`, `severity`), `disk_volumes[].encrypted`, `battery.condition` (`Normal` vs `Service Recommended`).
- **Not in telemetry (configured in `copilot.tenant_policies`)**: Low disk free %, sustained RAM usage %, early battery EOL cycle threshold, stale device window, and flagged software list.

### Live-Feed Simulator (`scripts/telemetry_sim.py`)

Lives outside `src/fleet_copilot/` and connects exclusively with `telemetry_writer` to simulate real-world ingestion:
- `seed`: Idempotently loads `data/device-telemetry-dataset.ndjson` (750 snapshots).
- `push <file.json>`: Inserts a single raw JSON snapshot (validates only the 5 core fields + `company_id`).
- `scenario <name>`: Reproducible edge cases (`partial-payload`, `duplicate-and-late`, `toctou-resolved`, `high-sev-alert`, `prompt-injection-software`). The UI's demo controls inject the same scenarios through `ops.inject_scenario()` (role `demo_ops`).
- `reset`: Atomically validates the dataset file, clears snapshots/companies, and re-seeds the 750-row baseline.

---

## 2. `copilot` Schema (Owned by Fleet Copilot)

### LangGraph Checkpointer Tables
Managed by the synchronous LangGraph `PostgresSaver.setup()` (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`), with an AES `EncryptedSerializer` (`LANGGRAPH_AES_KEY`) so checkpoint payloads are encrypted at rest. Automatically placed in `copilot` via `ALTER ROLE copilot_app SET search_path TO copilot`. Stores multi-turn state and durable paused `interrupt()` approvals.

> **Isolation note:** LangGraph's internal checkpointer tables do not have a `company_id` column and are not governed by Postgres RLS; tenant isolation for checkpoints is enforced via tenant-prefixed `thread_id` (`{company_id}:{uuid}`) verified against RLS-protected `copilot.threads` before every graph invoke/resume.

### Operational Tables

All tenant tables (`threads`, `turns`, `tenant_policies`, `tenant_tool_configs`, `action_proposals`, `audit_logs`, `audit_chain_heads`, `charts`, `integration_outbox`, `spend_ledger`) enforce fail-closed PostgreSQL RLS on `company_id` (`app.current_tenant`), non-wildcard `CHECK (company_id <> '*')` on single-tenant tables, and **composite tenant-scoped foreign keys** (`(company_id, thread_id)` and `(company_id, thread_id, turn_id)`) so cross-tenant FK linkage is rejected at the database level.

| Table | Key Columns | Purpose & Security |
|---|---|---|
| `copilot.threads` | `thread_id PK`, `company_id`, `actor_id`, `title`, `status`, `run_lease_until`, `hidden`, `created_at`, `updated_at`, `UNIQUE(company_id, thread_id)` | Binds each conversation thread to one `company_id` and `actor_id` (other actors get 404). `run_lease_until` is a compare-and-set lease so one thread runs one turn at a time. No runtime `DELETE` for `copilot_app`. |
| `copilot.turns` | `turn_id PK`, `trace_id`, `(company_id, thread_id) FK`, `actor_id`, `user_message`, `plan_json`, `tool_calls_json`, `evidence_json`, `assistant_response`, `status`, `llm_model`, `input_tokens`, `output_tokens`, `latency_ms`, `created_at`, `completed_at` | Structured, SQL-queryable per-turn trace for the UI, debugging, and evaluation suite without unpacking binary checkpoints. No runtime `DELETE` for `copilot_app`. |
| `copilot.tenant_policies` | `policy_id PK`, `company_id`, `scope_type`, `scope_value`, `metric_key`, `value_json`, `updated_by`, `updated_at` | Typed thresholds with precedence: `global (company_id='*')` → `tenant` → `device` (the seed ships 19 global defaults), plus `CHECK ((company_id = '*') = (scope_type = 'global'))`. Tenants read `'*'` + own rows and write/delete only own non-global rows. |
| `copilot.tenant_tool_configs` | `PK(company_id, tool_name)`, `enabled`, `requires_approval`, `min_role`, `adapter_config_json`, `updated_by`, `updated_at` | Per-tenant tool enablement, RBAC floor (`employee`/`viewer`/`it_admin`), and adapter config. Global `enabled = false` (`company_id = '*'`) acts as a platform kill-switch. |
| `copilot.action_proposals` | `proposal_id PK`, `batch_id`, `(company_id, thread_id, turn_id) FK`, `proposed_by_actor_id`, `tool_name`, `target_type`, `target_id`, `arguments_json`, `rationale`, `evidence_snapshot_ids`, `evidence_as_of`, `policy_check_json`, `status`, `reviewer_id`, `review_note`, `reviewed_at`, `external_receipt_json`, `executed_at`, `created_at` | Tracks full HITL lifecycle (`pending_approval` → `approved` / `rejected` / `refused_by_guardrail` / `stale_blocked` → `executed` / `failed`), batched approvals (`batch_id`), deduplication, and TOCTOU freshness (`evidence_as_of`). No runtime `DELETE` for `copilot_app`. |
| `copilot.audit_logs` | `audit_id BIGSERIAL PK`, `trace_id`, `thread_id`, `company_id`, `actor_id`, `actor_role`, `event_type`, `payload_json`, `created_at` | **Append-only at the DB permission level**: `copilot_app` has `SELECT, INSERT` only (`UPDATE`, `DELETE`, `TRUNCATE` revoked). |
| `copilot.audit_chain_heads` | `company_id PK`, `last_seq`, `last_hash` | Per-tenant head of the SHA-256 audit hash chain; lets `verify` detect edits, deletions and reordering. |
| `copilot.charts` | `chart_id PK`, `company_id`, `owner_actor_id`, `thread_id`, `turn_id`, `title`, `request_json`, `spec_json`, `data_json`, `evidence_snapshot_ids`, `telemetry_as_of`, `pinned` | Personal saved charts (RLS by company + owner). Refresh re-runs the stored request on current telemetry. |
| `copilot.integration_outbox` | `receipt_id PK`, `company_id`, `proposal_id`, `adapter`, `channel`, `payload_json`, `created_at`, `redacted_at` | Exact payload each mock adapter "sent", written in the same transaction as the `executed` status. |
| `copilot.spend_ledger` | `id PK`, `company_id`, `actor_id`, `rev_id`, `turn_id`, `cost_usd`, `created_at` | One row per live turn attempt; drives the cumulative demo spend cap and turn/rate limits. Written via `SECURITY DEFINER` `record_spend()`; survives demo resets. |

### Platform tables (no tenant data)

| Table | Purpose |
|---|---|
| `copilot.hardware_catalog` | Per-model upgradeability (RAM/storage/battery) with source URLs and confidence; drives refusals such as "soldered RAM". Read-only to the app. |
| `telemetry.tenant_lock_secret`, `copilot.tenant_lock_secret` | Owner-only HMAC key for the signed tenant lock. No app role can read them. |
| `ops.runtime_settings`, `ops.seed_snapshot_ids` | Owner-only: the `demo_mode` flag and the baseline snapshot set that demo resets restore. |

