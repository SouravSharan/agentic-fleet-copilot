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
