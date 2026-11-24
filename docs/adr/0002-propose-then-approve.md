# ADR 0002 — Actions are proposed, approved by a human, then re-checked

**Status:** accepted

## Context
The brief requires state-changing actions with explicit human approval, audit logs, and refusal without
sufficient evidence. An LLM should never be the component that decides a side effect happens.

## Decision
- Action tools (`create_upgrade_order`, `open_remediation_ticket`, `flag_device_for_replacement`,
  `notify_employee`) only **draft**. A deterministic policy gate evaluates each draft against the cited
  evidence, the hardware catalog and the global → tenant → device policy cascade, and returns allow /
  allow-with-warnings / refuse (with verified alternatives that are offered, never auto-staged).
- Allowed drafts are stored as `pending_approval` with an approval hash; the graph pauses with LangGraph
  `interrupt()`. Approval can come from the chat card, the company-wide queue, the CLI or the API.
- A database trigger (`proposals_guard()`) enforces legal status transitions with compare-and-set; expired
  proposals cannot be approved.
- On approval, execution re-runs the policy rule on **fresh** telemetry (TOCTOU); if the problem is gone the
  action is blocked as stale. The adapter runs outside the DB transaction with an idempotency key; the final
  status, receipt, outbox row and audit events commit together.
- Integrations are mock adapters that write the exact outbound payload to `copilot.integration_outbox`.

## Consequences
- No side effect without a recorded human decision; every step is in the hash-chained audit log.
- Every action costs a click — the intended trade-off for an IT admin tool.
- Swapping a mock adapter for a real ServiceNow / procurement client doesn't touch the agent.
