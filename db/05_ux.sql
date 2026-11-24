-- 05_ux.sql
-- Phase B / D UX schema additions: chat history (`turns.response_json`, `threads.hidden`),
-- personal saved charts (`copilot.charts`), and future Phase D tables (added idempotently).

-- ---------------------------------------------------------------------------
-- (a) Chat History Columns (Phase B1)
-- ---------------------------------------------------------------------------

ALTER TABLE copilot.threads
    ADD COLUMN IF NOT EXISTS hidden          BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS run_lease_until TIMESTAMPTZ;

ALTER TABLE copilot.turns
    ADD COLUMN IF NOT EXISTS response_json   JSONB;

CREATE INDEX IF NOT EXISTS idx_copilot_threads_tenant_actor_visible
    ON copilot.threads (company_id, actor_id, updated_at DESC)
    WHERE hidden = false;

-- ---------------------------------------------------------------------------
-- (b) Personal Saved Charts (Phase B2; RLS by company, personal owner_actor_id)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS copilot.charts (
    chart_id              TEXT PRIMARY KEY,
    company_id            TEXT NOT NULL CHECK (company_id <> '*'),
    owner_actor_id        TEXT NOT NULL,
    thread_id             TEXT,
    turn_id               TEXT,
    title                 TEXT NOT NULL,
    request_json          JSONB NOT NULL,
    spec_json             JSONB NOT NULL,
    data_json             JSONB NOT NULL,
    evidence_snapshot_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    telemetry_as_of       TIMESTAMPTZ NOT NULL,
    pinned                BOOLEAN NOT NULL DEFAULT false,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_copilot_charts_tenant_owner
    ON copilot.charts (company_id, owner_actor_id, pinned DESC, updated_at DESC);

ALTER TABLE copilot.charts ENABLE ROW LEVEL SECURITY;
ALTER TABLE copilot.charts NO FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS charts_tenant_isolation ON copilot.charts;
CREATE POLICY charts_tenant_isolation ON copilot.charts
    FOR ALL
    TO copilot_app
    USING (company_id = (SELECT copilot.current_tenant()))
    WITH CHECK (company_id = (SELECT copilot.current_tenant()));

REVOKE ALL ON copilot.charts FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON copilot.charts TO copilot_app;
