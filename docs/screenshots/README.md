# Fleet Copilot Desktop Screenshots

Captured at `1440×900` against a live local PostgreSQL 16 + Gemini 3.8 Flash session during the
full multi-persona reviewer walkthrough.

| File | What it shows |
|---|---|
| [`01-sign-in.png`](01-sign-in.png) | Access-code sign-in page with four isolated demo personas (`Acme · IT admin`, `Acme · Viewer`, `Globex · IT admin`, `Initech · IT admin`). |
| [`02-home-briefing.png`](02-home-briefing.png) | `Acme · IT admin` Home fleet briefing: KPI tiles, humanized policy thresholds, battery wear chart with 800-cycle dashed threshold, and `Ask about this` / `Propose this action` CTAs. |
| [`03-copilot-citation-popover.png`](03-copilot-citation-popover.png) | Copilot wide centered transcript, sidebar `Chats` rail, neutral avatars, compact citation chips, and an open `[E#]` provenance popover showing snapshot ID, timestamp, metric, and value. |
| [`04-copilot-inspector.png`](04-copilot-inspector.png) | Copilot two-column layout with the turn Inspector opened from the answer-card button showing all five tabs (`Plan`, `Tools`, `Evidence`, `Verify`, `Usage`). |
| [`05-copilot-proposal-card.png`](05-copilot-proposal-card.png) | Staged `open_remediation_ticket` action proposal card paused at human approval with `Awaiting approval` badge, expiry countdown, rationale, and policy threshold footer. |
| [`06-copilot-refusal-card.png`](06-copilot-refusal-card.png) | `Globex · IT admin` policy refusal for a 32 GB RAM upgrade on a soldered ThinkPad X1 Carbon Gen 11 with verified catalog alternatives and `Stage this instead` controls. |
| [`07-copilot-chart-answer.png`](07-copilot-chart-answer.png) | Inline 30-day low-disk Vega-Lite line chart with single title, `Pin to Saved charts` button, and dashed 15% policy threshold rule. |
| [`08-copilot-sql-answer.png`](08-copilot-sql-answer.png) | Governed read-only SQL answer (`[Q1]`) computing average battery cycle count by hardware model with an expandable query and results table. |
| [`09-approvals-pending.png`](09-approvals-pending.png) | Approvals & Activity → `Pending approvals` queue with company-wide staged proposal, evidence snapshot chips, `Open chat`, and `Approve` / `Reject` buttons. |
| [`10-approvals-history.png`](10-approvals-history.png) | Approvals & Activity → `History` showing executed proposals with reviewer, decision channel (`queue` vs `chat`), timestamps, receipts, and outbox filter links. |
| [`11-approvals-audit-chain.png`](11-approvals-audit-chain.png) | Approvals & Activity → `Audit log` displaying the `Audit chain verified ✓` banner from `copilot.verify_audit_chain()`, event/thread filters, and humanized audit timeline. |
| [`12-approvals-outbox.png`](12-approvals-outbox.png) | Approvals & Activity → `Integration outbox` displaying the mock notice, formatted `Employee email` message preview card, `ServiceNow ticket` card, and raw JSON payloads. |
| [`13-fleet-devices-table.png`](13-fleet-devices-table.png) | Fleet → `Devices` filterable inventory table with disk/RAM progress bars, `Low`/`OK` policy indicator, battery condition, and device picker. |
| [`14-fleet-device-page.png`](14-fleet-device-page.png) | Fleet inline Device detail view showing device profile, 30-day Disk/RAM/Battery resource trend charts with policy threshold lines, and 30-day compliance timeline. |
| [`15-fleet-saved-charts.png`](15-fleet-saved-charts.png) | Fleet → `Saved charts` displaying the pinned 30-day low-disk chart with `Refresh`, `Download spec (JSON)`, `Download data (CSV)`, `Open source chat`, and `Delete`. |
| [`16-how-architecture.png`](16-how-architecture.png) | How it works → `Architecture` showing the LangGraph execution graph (highlighting the latest turn's visited nodes), system & tenancy diagram, and 13-tool catalog. |
| [`17-how-guardrails.png`](17-how-guardrails.png) | How it works → `Guardrails` interactive security playground with 8 adversarial scenarios, defense-layer notes, and instant `$0` recorded refusal results. |
| [`18-how-evaluation.png`](18-how-evaluation.png) | How it works → `Evaluation` showing the 4 hard release gates (100% PASS), 4 quality targets (including transparent `BELOW TARGET` badge on `QA correctness`), and `make eval` reproduction note. |
| [`19-how-examples.png`](19-how-examples.png) | How it works → `Examples` read-only gallery of all 51 recorded evaluation runs rendered via the shared Answer card with a `Recorded example` badge. |
| [`20-how-demo-controls.png`](20-how-demo-controls.png) | How it works → `Demo controls` (`it_admin` only) with `toctou-resolved` and `prompt-injection-software` scenario injectors and `Reset this company's demo` backed by `demo_ops`. |
| [`21-viewer-home.png`](21-viewer-home.png) | `Acme · Viewer` Home briefing proving strict read-only RBAC: same Acme fleet briefing with zero `Propose this action` controls rendered. |
