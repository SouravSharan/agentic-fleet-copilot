# Telemetry Dataset

- **Source:** Provided in the Rayda Senior Agentic AI Engineer take-home brief ([Google Drive link](https://drive.google.com/file/d/15HqoteWcUOAEy6aNr0JPjfdM-DMQPd2P/view?usp=sharing)).
- **File:** `device-telemetry-dataset.ndjson`
- **SHA-256:** `fb0ee31ffeeb731b919fc4d5124098c9c5b68cf3fba35eed5092a9f73655c4de`
- **Shape:** 750 NDJSON snapshots, 25 devices, 3 companies (`acme-001`, `globex-002`, `initech-003`), 14 employees.
- **Window:** `2026-05-14T08:40:00Z` → `2026-06-12T11:00:00Z` (30 daily snapshots per device).

Committed directly so fresh clones and CI can run `make up migrate seed test` and replayable evals without manual downloads. The repository remains private.
