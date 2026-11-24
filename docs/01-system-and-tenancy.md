# 01 · System Boundaries & Tenancy Model

## System Boundary

The Fleet Copilot is a tenant-scoped reasoning and action-orchestration service. It sits between an external fleet telemetry store and external operational systems (procurement, ticketing, MDM, employee notifications).

```mermaid
flowchart LR
    subgraph External["External (Outside Copilot Control)"]
        AGENTS["Desktop Agents / Telemetry Feed"]
        TDB[("Platform Telemetry Store (schema: telemetry, read-only)")]
        APIS["External Action APIs (Procurement, ITSM, MDM, Comms)"]
    end

    subgraph Copilot["Fleet Copilot Service"]
        API["API / CLI / Web UI"]
        ORCH["LangGraph Orchestrator & Policy Gate"]
        CDB[("Copilot Store (schema: copilot, read/write)")]
    end

    AGENTS --> TDB
    API --> ORCH
    ORCH -- "SELECT only (telemetry_ro + RLS)" --> TDB
    ORCH <--> CDB
    ORCH -- "Typed API call (post-HITL only)" --> APIS
```

### Ownership Split

| Component | Owner | Responsibility |
|---|---|---|
| **Telemetry Store (`telemetry` schema)** | External platform | Receives raw point-in-time JSON snapshots from desktop agents. Copilot has read-only access and does not own ingestion. |
| **Copilot Store (`copilot` schema)** | Fleet Copilot | Stores conversation threads, LangGraph checkpoints, per-turn traces, tenant policies, tool configs, action proposals, and append-only audit logs. |
| **Tools** | Fleet Copilot | Copilot owns tool definitions, input/output schemas, validation, policy gating, and execution wrappers. For external action APIs, Copilot owns the request/response contract and error handling—nothing downstream of that API. |

---

## Entities vs. Tenants

Only `company_id` is a tenant boundary. Devices, employees, and users exist *inside* a tenant.

| Identifier | What It Represents | Scope |
|---|---|---|
| `company_id` | Customer organization (e.g. `acme-001`, `globex-002`, `initech-003`) | **Tenant boundary** — hard isolation unit across DB, threads, memory, policies, and audit logs. |
| `device_id` | Managed hardware asset (laptop or desktop) | Resource owned by one `company_id`. |
| `employee_id` | Staff member who holds 0, 1, or multiple devices | Resource owned by one `company_id`; target of `notify_employee`. |
| `actor_id` + `role` | Authenticated principal interacting with the Copilot | Operates inside one active `company_id` per session. |

---

## Stakeholders & Access Control

| Actor / Role | Scope | Capabilities | Restrictions |
|---|---|---|---|
| **Customer `it_admin`** | Single `company_id` | Fleet QA, trend analysis, propose actions, approve/reject pending actions for their company, view company audit logs, manage own tenant thresholds & tool toggles. | Zero access to other companies' telemetry, threads, proposals, or audit logs. Cannot modify global platform defaults (`company_id = '*'`). |
| **Customer `viewer`** | Single `company_id` | Read-only fleet QA, trend analysis, view company audit logs. | State-changing action tools are not bound to the LLM. Cannot approve/reject actions or edit policies. |
| **Rayda `platform_admin`** | Control Plane (outside tenant chat) | Adds/updates tools via code & deployments, sets global defaults and tool kill-switches (`company_id = '*'`), inspects operational health and trace metadata. | Deny-by-default on customer telemetry. Debugging customer issues requires an explicit single-tenant, read-only break-glass session logged in `audit_logs`. Never executes customer state-changing actions. |

**Future-compatible principals (same model, no schema changes required):**
- **`employee` (self-service)**: Scoped to `company_id` + own `employee_id` devices only.
- **`service` (scheduled monitor)**: Runs one isolated job per `company_id`; proposal-only (queues `pending_approval` actions for human `it_admin` review).

---

## Core Security Invariants

1. **Single-tenant session invariant**: Every API request, chat thread, DB transaction, and LLM context window is bound to **one** verified `company_id`. Even if a user belongs to multiple companies (e.g. an MSP admin), switching companies requires an explicit workspace switch into a new isolated session. Two companies are never loaded into the same LLM context.
2. **No cross-tenant meta-agent**: Platform administration (tool registration, global kill-switches, policy defaults) stays deterministic via code, config, and typed APIs—not behind an LLM with cross-tenant access.
3. **Defense-in-depth tenant isolation**:
   - **API boundary**: `company_id`, `actor_id`, and `role` come from verified request auth, never from the user prompt. `thread_id` is bound to `company_id`.
   - **Agent runtime**: Injected as immutable LangGraph `context_schema` (`TenantContext`); `company_id` is never an LLM-controlled tool argument.
   - **Database layer**: Separate least-privilege PostgreSQL roles (`telemetry_ro` vs `copilot_app`) with fail-closed Row-Level Security (`app.current_tenant`).
   - **No existence oracle**: Looking up a `device_id` that belongs to another company returns `not_found` inside the caller's tenant rather than confirming the ID exists elsewhere.
4. **Intra-company visibility**: Chat threads are scoped to `(company_id, actor_id)`; the action approval queue, policies, and audit logs are scoped to `company_id` so any `it_admin` within the same company can review and audit actions.
5. **No admin/superuser credentials in the web app**: Demo resets and scenario injection run through admin-owned `SECURITY DEFINER` functions (`ops.demo_reset_company`, `ops.inject_scenario`) executed by a dedicated `NOBYPASSRLS` login role (`demo_ops`, zero direct table privileges) gated by an owner-only `ops.runtime_settings` table row synced from `DEMO_MODE`.

---

## Demo & Runtime Environment Variables

| Variable | Default (Dev) | Purpose |
|---|---|---|
| `APP_ENV` | `dev` | `dev` allows access code `"dev"` when `DEMO_ACCESS_CODES` is empty; `prod` refuses startup without configured codes. |
| `DEMO_MODE` | `true` | Written into owner-only `ops.runtime_settings` during `ops.migrate`; `ops.demo_reset_company` and `ops.inject_scenario` refuse when `false`. |
| `DEMO_OPS_DATABASE_URL` | `postgresql://demo_ops:...@localhost:5432/fleet_copilot` | Least-privilege login DSN used by the web app for demo resets and scenario injection (zero table grants). |
| `DEMO_OPS_PASSWORD` | `demo_ops` | Password synced onto the `demo_ops` role during `ops.migrate`. |
| `DEMO_ACCESS_CODES` | *(empty in dev)* | Comma-separated `code:rev_id` pairs validated in constant time (`hmac.compare_digest`). |
| `SESSION_SIGNING_KEY` | *(dev 64-hex in `.env.example`)* | HS256 JWT signing key for API demo sessions (8 h expiry; stripped from logs). |
| `DEMO_SPEND_CAP_USD` | `2.00` | Hard cumulative live spend cap (with `$0.05` reserve) persisted in `copilot.spend_ledger`. |
| `DEMO_TURNS_PER_CODE` | `25` | Lifetime live turn limit per reviewer access code (`rev_id`). |
| `DEMO_TURNS_PER_HOUR` | `20` | Rolling 1-hour live turn rate limit per `(company_id, actor_id)`. |
