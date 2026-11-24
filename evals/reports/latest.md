# Fleet Copilot Evaluation Report (PASS)

- **Date (UTC):** `2026-11-24T11:09:28.018808+00:00`
- **Model:** `google_genai:gemini-3.8-flash`
- **Mode:** `replay` · **Repeats:** `1`
- **Git SHA:** `3a84828` · **Cassette hash:** `9fd547d163f52e2b`

## CI Gates

| Gate | Actual | Target | Status |
|---|---:|---:|:---:|
| ★ citation_validity | 1.0000 | >= 1.00 | PASS |
| ★ safety | 1.0000 | >= 1.00 | PASS |
| ★ audit_verify | 1.0000 | >= 1.00 | PASS |
| ★ privacy_scan | 1.0000 | >= 1.00 | PASS |
| qa_correctness | 1.0000 | >= 0.95 | PASS |
| insights_recall | 1.0000 | >= 1.00 | PASS |
| insights_precision | 1.0000 | >= 0.95 | PASS |
| insight_structure | 1.0000 | >= 1.00 | PASS |
| action_accuracy | 1.0000 | >= 0.90 | PASS |

## Summary Metrics

| Metric | Value |
|---|---:|
| Cases / Total runs | 51 / 77 |
| Overall pass rate | 1.0000 |
| Stability (`pass^1`) | 1.0000 |
| QA correctness (16 runs) | 1.0000 |
| Insights recall / precision (5 runs) | 1.0000 / 1.0000 |
| Action accuracy (10 runs) | 1.0000 |
| Multi-turn pass rate (5 runs) | 1.0000 |
| Red-team pass rate (39 runs) | 1.0000 |
| Citation coverage | 1.0000 |
| Numeric faithfulness (independent) | 0.9974 |
| First-pass grounding rate | 0.9870 |
| Verifier retries / claims stripped | 1 / 0 |
| Trajectory pass rate | 1.0000 |
| Total tokens (in / out) | 1,083,059 / 46,718 |
| Recorded LLM cost (`recorded_cost`, USD) | $0.9875 |
| Mean latency (ms) | 624.0 |

## Failures

None — all cases passed every repeat.
