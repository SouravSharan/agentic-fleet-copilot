# Telemetry Dataset

- **Source:** Synthetic device telemetry provided with the assessment (no real people or devices).
- **File:** `device-telemetry-dataset.ndjson`
- **SHA-256:** `fb0ee31ffeeb731b919fc4d5124098c9c5b68cf3fba35eed5092a9f73655c4de`
- **Shape:** 750 NDJSON snapshots, 25 devices, 3 companies (`acme-001`, `globex-002`, `initech-003`), 14 employees.
- **Window:** `2026-05-14T08:40:00Z` → `2026-06-12T11:00:00Z` (30 daily snapshots per device).

Committed so fresh clones, Docker and CI can seed the database and replay the evals without manual downloads.
