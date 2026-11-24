# ADR 0003 — Evidence handles and a deterministic verifier

**Status:** accepted

## Context
Answers must be traceable and must not contain unsupported conclusions. Asking the model to "cite sources"
is not enforceable; an LLM judge is slow, costly and itself unverified.

## Decision
- Tools return computed results (counts, thresholds, trends) and register evidence in a per-turn ledger.
  Each item gets a handle — `[E#]` for a snapshot metric, `[C#]` for a chart's rows, `[Q#]` for a SQL result —
  bound to real `snapshot_id`s, timestamps and values.
- The synthesizer must return a structured `Answer`: summary, claims (each with citation handles),
  `result_device_ids`, caveats.
- `verify_grounding` checks deterministically. Hard: handles exist; every device named or returned appears in
  cited evidence. Soft: numbers in claims and the summary are matched against evidence values and flagged as
  warnings. On a hard violation: one corrective retry, then the answer ships with caveats. `finalize` neutralises any unknown handle that slipped through.
- The UI makes every handle clickable (snapshot id, metric, value, time) and the Inspector shows the ledger.

## Consequences
- "Citation validity" is a hard eval gate at 1.0, measured on every run.
- The model can't cite what the tools didn't return, so new kinds of questions need tools (or the governed
  SQL tool, which produces `[Q#]` evidence).
