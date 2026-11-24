# ADR 0004 — Record/replay evaluation with deterministic graders

**Status:** accepted

## Context
The brief asks for a runnable evaluation suite and reasonably deterministic execution. Live LLM evals are
non-deterministic, cost money on every run, and need an API key reviewers may not have.

## Decision
- Cases are YAML (`evals/cases/`): QA, insights, actions, multi-turn, charts, SQL and red-team. Expected
  answers come from a deterministic **oracle** computed from the dataset (`evals/oracle.py`), not hand-typed.
- Graders are code, not an LLM judge: correctness vs oracle, citation validity, safety, audit-chain
  verification, privacy scan, insight recall/precision, action accuracy.
- A recording run stores every model request/response as cassettes keyed by a hash of model, messages,
  tools, schemas and config. `make eval` replays them offline in about a minute for $0, and CI runs it on
  every push. Red-team cases run three times (pass^3).
- Hard gates (citation validity, safety, audit verify, privacy) fail the run; quality targets are reported.
  No per-case re-rolls: one recording, reported as-is.

## Consequences
- Reproducible and free to re-run; reviewers can run it without a key.
- Any change to prompts, tool schemas or the answer schema invalidates cassettes and needs a paid
  re-record (about $1) — so agent-surface changes are batched.
- Replay proves the agent's decisions for recorded inputs; it does not measure drift on new data (online
  evals are on the roadmap).
