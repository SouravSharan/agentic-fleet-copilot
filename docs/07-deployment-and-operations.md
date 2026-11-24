# 07 — Deployment and operations

One container image runs every process: the Streamlit UI (default command), the one-shot database
setup, and the nightly maintenance job. Two Compose files describe the two ways to run it.

| File | Use | Ports | Data |
|---|---|---|---|
| `docker-compose.yml` | Local review and development | `127.0.0.1:8501` (UI), `127.0.0.1:5432` (Postgres) | Docker volume `fleet_pgdata` |
| `docker-compose.yml` + `docker-compose.prod.yml` | Hosted demo | none — reached only through a reverse proxy | Host directory `PGDATA_DIR` |

## Services

| Service | Image / command | Credentials it receives | Notes |
|---|---|---|---|
| `postgres` | `postgres:16-alpine` | owner (its own) | Prod: internal-only network, 512 MB cap |
| `setup` | app image: `python -m fleet_copilot.ops.migrate && python scripts/telemetry_sim.py seed` | owner DSN + role passwords | One-shot and idempotent: roles, schemas, RLS, triggers, signed-lock secret, hardware catalog, 750 seed snapshots |
| `ui` | app image: `streamlit run …` | **only** `telemetry_ro`, `copilot_app`, `demo_ops` DSNs | Never sees the owner or writer DSN. Runs as uid 10001. Healthcheck on `/_stcore/health` |
| `nightly` (prod) | app image: `fleet-copilot nightly` once a day | owner DSN | Expires stale proposals, recovers stuck executions, resets demo tenants (spend ledger is kept) |

The UI's environment is an explicit allow-list in `docker-compose.yml` — it does not load `.env` wholesale,
so secrets meant for `setup` never reach the web process. A test (`test_ui_process_needs_no_admin_or_writer_dsn`)
proves the UI renders every page with the owner and writer DSNs pointing at an unreachable host.

## Local

```bash
cp .env.example .env
docker compose up --build          # http://localhost:8501, access code "dev"
docker compose down                # keep data;  add -v to wipe the database volume
```

Without `GEMINI_API_KEY` the UI runs in **examples mode**: recorded runs, guardrail demos, eval report and all
fleet pages work; live chat is disabled with a clear message.

`make up` starts only Postgres for the `uv` workflow (`make bootstrap`, `make ui`, `make test`).

## Hosted demo

The demo runs on a small VPS behind a Cloudflare tunnel and Nginx Proxy Manager (NPM):

```
browser ──HTTPS──▶ Cloudflare ──tunnel──▶ NPM container ──proxy network──▶ fleet-copilot-ui:8501
                                                                              │ db network (internal)
                                                                              ▼
                                                                          postgres
```

- No container publishes a host port; the server's public IP has no open app ports.
- Postgres is on a Docker network marked `internal: true` (no egress, not reachable from the proxy network).
- The UI joins three networks: `db`, `default` (outbound HTTPS to the Gemini API) and the proxy's network.

### Steps

1. Clone the repository on the server and create `.env` from `.env.example` with **fresh** values:
   `openssl rand -hex 32` for `TENANT_LOCK_KEY`, `PSEUDONYM_KEY`, `SESSION_SIGNING_KEY`;
   `openssl rand -hex 16` for `LANGGRAPH_AES_KEY`; new passwords for `POSTGRES_PASSWORD` and the four role
   passwords; `APP_ENV=prod`; `DEMO_ACCESS_CODES=<code>:<reviewer_id>,…`; `DEMO_SPEND_CAP_USD`;
   `GEMINI_API_KEY`; `PGDATA_DIR=<host path>`; `PROXY_NETWORK=<proxy's docker network>`.
   `chmod 600 .env`.
2. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`
3. In NPM add a proxy host: domain → `fleet-copilot-ui`, port `8501`, **Websockets support on**
   (Streamlit needs it), "Block common exploits" on. Route the hostname through the tunnel.
4. Smoke test: sign in with each persona, ask a cross-tenant question (must refuse), check a viewer has no
   action buttons, and confirm `docker ps` shows no published ports.

### Operations

| Task | Command |
|---|---|
| Logs | `docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f ui` |
| Update | `git pull && docker compose -f … -f … up -d --build` (setup re-runs, idempotent) |
| Reset one tenant's demo | UI → How it works → Demo controls (admin), or `fleet-copilot nightly` |
| Spend so far | `SELECT company_id, sum(cost_usd) FROM copilot.spend_ledger GROUP BY 1;` (owner) |
| Backup | `docker exec fleet-copilot-postgres pg_dump -U postgres fleet_copilot > backup.sql` |
| Stop | `docker compose -f … -f … down` (data stays in `PGDATA_DIR`) |

### Limits that protect the hosted demo

- Cumulative spend cap (`DEMO_SPEND_CAP_USD`) stored in Postgres, checked before every live turn, with a
  reserve; failed turns are charged a reserve so retries can't drain the budget for free.
- Per-access-code turn cap and an hourly rate limit per actor.
- When a limit is hit the app switches to examples mode instead of erroring.
- Memory caps per container so the demo can't starve other services on the host.
