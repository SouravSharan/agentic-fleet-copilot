# Demo script (3–4 minutes)

Record at 1440×900, light theme. Live URL or local stack with a Gemini key. Reset the Acme demo first
(How it works → Demo controls → Reset this company's demo). Speak in short sentences; let the UI show the detail.

| Time | Screen | Say |
|---|---|---|
| 0:00–0:20 | Sign-in page | "Fleet Copilot turns device telemetry into decisions and actions. Three companies, four personas, each fully isolated. I'll sign in as Acme's IT admin." |
| 0:20–0:45 | Home | "Before I ask anything, the briefing is computed deterministically from 30 days of telemetry — batteries near end-of-life, low disk, RAM pressure, compliance drift. Each card has the finding, the evidence and a short explanation." |
| 0:45–1:30 | Copilot: *"Which Acme laptops have batteries needing service, and what should we do?"* | "This is a LangGraph agent: a planner, a tool loop, a synthesizer and a deterministic verifier. Every fact cites evidence — this `[E2]` is a real snapshot with its timestamp. The Inspector shows the plan, each tool call, the evidence ledger, the verifier result and the cost." |
| 1:30–2:15 | Copilot: *"Open a screen_lock remediation ticket for acme-macbook-2"* → Approve | "Actions are only proposed. The policy gate checked the evidence; nothing happens until I approve. On approval it re-checks fresh telemetry, then the mock ServiceNow adapter runs. Here's the receipt, the exact outbound payload, and the hash-chained audit log — verified." |
| 2:15–2:45 | Sign in as Globex: *"Order a 32GB RAM upgrade for globex-thinkpad-6"*, then *"Show me Acme's devices"* | "It refuses an upgrade the hardware can't take — soldered RAM — and offers alternatives. And it refuses another company's data before any tool runs; Postgres row-level security with a signed tenant lock is the backstop." |
| 2:45–3:30 | How it works → Evaluation, Examples | "Evaluation is a record/replay suite: 51 core cases, red-team cases three times, deterministic graders, hard gates in CI. It replays offline for free. Every case passes now, and the report shows the raw first recording next to the re-graded result, so nothing is hidden." |
| 3:30–3:50 | README | "Everything runs with one `docker compose up`, even without an API key. Thanks for watching." |

Backup if live AI is slow: use How it works → Examples, which renders the same recorded runs.
