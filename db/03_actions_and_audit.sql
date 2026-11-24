-- 03_actions_and_audit.sql
-- Action-layer schema: hardware catalog, immutable proposal lifecycle + dedupe index,
-- per-tenant tamper-evident audit hash chain, and turn metadata columns.

-- ---------------------------------------------------------------------------
-- (a) Hardware Catalog (platform reference data; no RLS; read-only to copilot_app)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS copilot.hardware_catalog (
    model_identifier    TEXT PRIMARY KEY,
    model_name          TEXT NOT NULL,
    form_factor         TEXT NOT NULL CHECK (form_factor IN ('laptop', 'desktop', 'unknown')),
    ram_upgradable      BOOLEAN NOT NULL,
    ram_max_gb          INTEGER,
    storage_upgradable  BOOLEAN NOT NULL,
    battery_serviceable BOOLEAN NOT NULL,
    source_urls         JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence          TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    verified_at         DATE NOT NULL
);

REVOKE ALL ON copilot.hardware_catalog FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;
GRANT SELECT ON copilot.hardware_catalog TO copilot_app;

-- ---------------------------------------------------------------------------
-- (b) Action Proposals: new lifecycle columns + strict status CHECK
-- ---------------------------------------------------------------------------

ALTER TABLE copilot.action_proposals
    ADD COLUMN IF NOT EXISTS dedupe_key      TEXT,
    ADD COLUMN IF NOT EXISTS approval_hash   TEXT,
    ADD COLUMN IF NOT EXISTS policy_version  TEXT,
    ADD COLUMN IF NOT EXISTS expires_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS decided_via     TEXT,
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT,
    ADD COLUMN IF NOT EXISTS executing_since TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS attempts        INTEGER NOT NULL DEFAULT 0;

DO $$
BEGIN
    ALTER TABLE copilot.action_proposals DROP CONSTRAINT IF EXISTS action_proposals_status_check;
    ALTER TABLE copilot.action_proposals
        ADD CONSTRAINT action_proposals_status_check
        CHECK (status IN (
            'pending_approval',
            'approved',
            'rejected',
            'expired',
            'executing',
            'executed',
            'failed',
            'stale_blocked'
        ));

    ALTER TABLE copilot.action_proposals DROP CONSTRAINT IF EXISTS action_proposals_decided_via_chk;
    ALTER TABLE copilot.action_proposals
        ADD CONSTRAINT action_proposals_decided_via_chk
        CHECK (decided_via IS NULL OR decided_via IN ('chat', 'queue', 'api'));
END $$;

-- ---------------------------------------------------------------------------
-- (c) Proposal Immutability & State-Machine Guard Trigger
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION copilot.proposals_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, copilot
AS $$
BEGIN
    -- 1. Terminal rows are completely immutable: any UPDATE on a terminal status raises.
    IF OLD.status IN ('rejected', 'expired', 'executed', 'failed', 'stale_blocked') THEN
        RAISE EXCEPTION 'terminal proposal rows are immutable (status=%)', OLD.status
            USING ERRCODE = '42501';
    END IF;

    -- 2. Freeze all 20 payload/identity/TTL columns after insertion.
    IF NEW.proposal_id           IS DISTINCT FROM OLD.proposal_id
    OR NEW.batch_id              IS DISTINCT FROM OLD.batch_id
    OR NEW.thread_id             IS DISTINCT FROM OLD.thread_id
    OR NEW.turn_id               IS DISTINCT FROM OLD.turn_id
    OR NEW.company_id            IS DISTINCT FROM OLD.company_id
    OR NEW.proposed_by_actor_id  IS DISTINCT FROM OLD.proposed_by_actor_id
    OR NEW.tool_name             IS DISTINCT FROM OLD.tool_name
    OR NEW.target_type           IS DISTINCT FROM OLD.target_type
    OR NEW.target_id             IS DISTINCT FROM OLD.target_id
    OR NEW.arguments_json        IS DISTINCT FROM OLD.arguments_json
    OR NEW.rationale             IS DISTINCT FROM OLD.rationale
    OR NEW.evidence_snapshot_ids IS DISTINCT FROM OLD.evidence_snapshot_ids
    OR NEW.evidence_as_of        IS DISTINCT FROM OLD.evidence_as_of
    OR NEW.policy_check_json     IS DISTINCT FROM OLD.policy_check_json
    OR NEW.dedupe_key            IS DISTINCT FROM OLD.dedupe_key
    OR NEW.approval_hash         IS DISTINCT FROM OLD.approval_hash
    OR NEW.policy_version        IS DISTINCT FROM OLD.policy_version
    OR NEW.expires_at            IS DISTINCT FROM OLD.expires_at
    OR NEW.idempotency_key       IS DISTINCT FROM OLD.idempotency_key
    OR NEW.created_at            IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'action_proposals payload columns are immutable'
            USING ERRCODE = '42501';
    END IF;

    -- 3. Enforce legal status transitions.
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF OLD.status = 'pending_approval'
           AND NEW.status IN ('approved', 'rejected', 'expired') THEN
            NULL;
        ELSIF OLD.status = 'approved'
           AND NEW.status IN ('executing', 'stale_blocked', 'failed') THEN
            NULL;
        ELSIF OLD.status = 'executing'
           AND NEW.status IN ('executed', 'failed', 'stale_blocked') THEN
            NULL;
        ELSE
            RAISE EXCEPTION 'illegal proposal status transition: % -> %', OLD.status, NEW.status
                USING ERRCODE = 'P0001';
        END IF;
    END IF;

    -- 4. Transition-gate mutable decision, execution, and outcome fields.
    IF (NEW.reviewer_id IS DISTINCT FROM OLD.reviewer_id
        OR NEW.reviewed_at IS DISTINCT FROM OLD.reviewed_at
        OR NEW.review_note IS DISTINCT FROM OLD.review_note
        OR NEW.decided_via IS DISTINCT FROM OLD.decided_via)
       AND NOT (OLD.status = 'pending_approval' AND NEW.status IN ('approved', 'rejected')) THEN
        RAISE EXCEPTION 'reviewer fields may only change on pending_approval -> approved|rejected'
            USING ERRCODE = '42501';
    END IF;

    IF NEW.executing_since IS DISTINCT FROM OLD.executing_since
       AND NOT (OLD.status = 'approved' AND NEW.status = 'executing') THEN
        RAISE EXCEPTION 'executing_since may only change on approved -> executing'
            USING ERRCODE = '42501';
    END IF;

    IF (NEW.external_receipt_json IS DISTINCT FROM OLD.external_receipt_json
        OR NEW.executed_at IS DISTINCT FROM OLD.executed_at)
       AND NOT (
           NEW.status IS DISTINCT FROM OLD.status
           AND NEW.status IN ('executed', 'failed', 'stale_blocked')
       ) THEN
        RAISE EXCEPTION 'outcome fields may only change when transitioning into a terminal status'
            USING ERRCODE = '42501';
    END IF;

    IF NEW.attempts IS DISTINCT FROM OLD.attempts THEN
        IF NOT (OLD.status = 'executing' AND NEW.attempts = OLD.attempts + 1) THEN
            RAISE EXCEPTION 'attempts may only increment by 1 while executing'
                USING ERRCODE = '42501';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_proposals_guard ON copilot.action_proposals;
CREATE TRIGGER trg_proposals_guard
    BEFORE UPDATE ON copilot.action_proposals
    FOR EACH ROW
    EXECUTE FUNCTION copilot.proposals_guard();

-- ---------------------------------------------------------------------------
-- (d) Active-Proposal Partial Unique Index (deduplication at DB layer)
-- ---------------------------------------------------------------------------

CREATE UNIQUE INDEX IF NOT EXISTS uq_copilot_proposals_active_dedupe
    ON copilot.action_proposals (company_id, tool_name, dedupe_key)
    WHERE status IN ('pending_approval', 'approved', 'executing');

-- ---------------------------------------------------------------------------
-- (e) Per-Tenant Tamper-Evident Audit Hash Chain
-- ---------------------------------------------------------------------------

ALTER TABLE copilot.audit_logs
    ADD COLUMN IF NOT EXISTS seq_no    BIGINT,
    ADD COLUMN IF NOT EXISTS prev_hash BYTEA,
    ADD COLUMN IF NOT EXISTS row_hash  BYTEA;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'copilot.audit_logs'::regclass
          AND conname = 'audit_logs_company_seq_uniq'
    ) THEN
        ALTER TABLE copilot.audit_logs
            ADD CONSTRAINT audit_logs_company_seq_uniq UNIQUE (company_id, seq_no);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS copilot.audit_chain_heads (
    company_id TEXT PRIMARY KEY CHECK (company_id <> '*'),
    last_seq   BIGINT NOT NULL DEFAULT 0,
    last_hash  BYTEA NOT NULL DEFAULT decode(
        '0000000000000000000000000000000000000000000000000000000000000000',
        'hex'
    )
);

ALTER TABLE copilot.audit_chain_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE copilot.audit_chain_heads NO FORCE ROW LEVEL SECURITY;
REVOKE ALL ON copilot.audit_chain_heads FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;

CREATE OR REPLACE FUNCTION copilot.audit_chain_before_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, copilot
AS $$
DECLARE
    v_last_seq  BIGINT;
    v_last_hash BYTEA;
    v_canonical TEXT;
BEGIN
    -- Ensure a head row exists for this tenant, then lock it to serialise per-tenant writes.
    INSERT INTO copilot.audit_chain_heads (company_id)
    VALUES (NEW.company_id)
    ON CONFLICT (company_id) DO NOTHING;

    SELECT last_seq, last_hash
      INTO v_last_seq, v_last_hash
      FROM copilot.audit_chain_heads
     WHERE company_id = NEW.company_id
       FOR UPDATE;

    -- Always overwrite client-supplied timestamps, sequence numbers, and hashes.
    NEW.created_at := now();
    NEW.seq_no := v_last_seq + 1;
    NEW.prev_hash := v_last_hash;
    NEW.payload_json := COALESCE(NEW.payload_json, '{}'::jsonb);

    v_canonical := concat_ws(
        '|',
        NEW.company_id,
        NEW.seq_no::text,
        NEW.trace_id,
        COALESCE(NEW.thread_id, ''),
        NEW.actor_id,
        NEW.actor_role,
        NEW.event_type,
        NEW.payload_json::text,
        to_char(NEW.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
    );

    NEW.row_hash := pg_catalog.sha256(NEW.prev_hash || convert_to(v_canonical, 'UTF8'));

    UPDATE copilot.audit_chain_heads
       SET last_seq = NEW.seq_no,
           last_hash = NEW.row_hash
     WHERE company_id = NEW.company_id;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION copilot.audit_chain_before_insert()
    FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;

DROP TRIGGER IF EXISTS trg_audit_chain_before_insert ON copilot.audit_logs;
CREATE TRIGGER trg_audit_chain_before_insert
    BEFORE INSERT ON copilot.audit_logs
    FOR EACH ROW
    EXECUTE FUNCTION copilot.audit_chain_before_insert();

CREATE OR REPLACE FUNCTION copilot.audit_chain_head()
RETURNS TABLE(last_seq BIGINT, last_hash BYTEA)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, copilot
AS $$
DECLARE
    v_tenant TEXT;
BEGIN
    v_tenant := copilot.current_tenant();
    IF v_tenant IS NULL THEN
        RETURN;
    END IF;
    RETURN QUERY
    SELECT h.last_seq, h.last_hash
      FROM copilot.audit_chain_heads AS h
     WHERE h.company_id = v_tenant;
END;
$$;

REVOKE ALL ON FUNCTION copilot.audit_chain_head()
    FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;
GRANT EXECUTE ON FUNCTION copilot.audit_chain_head() TO copilot_app;

CREATE OR REPLACE FUNCTION copilot.verify_audit_chain()
RETURNS TABLE(ok BOOLEAN, rows_checked BIGINT, first_bad_seq BIGINT)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, copilot
AS $$
DECLARE
    v_genesis       CONSTANT BYTEA := decode(
        '0000000000000000000000000000000000000000000000000000000000000000',
        'hex'
    );
    v_expected_seq  BIGINT := 0;
    v_expected_prev BYTEA := v_genesis;
    v_checked       BIGINT := 0;
    v_canonical     TEXT;
    v_calc_hash     BYTEA;
    v_head_seq      BIGINT;
    v_head_hash     BYTEA;
    r               RECORD;
BEGIN
    SELECT h.last_seq, h.last_hash
      INTO v_head_seq, v_head_hash
      FROM copilot.audit_chain_head() AS h;

    FOR r IN
        SELECT
            a.company_id,
            a.seq_no,
            a.trace_id,
            a.thread_id,
            a.actor_id,
            a.actor_role,
            a.event_type,
            a.payload_json,
            a.created_at,
            a.prev_hash,
            a.row_hash
        FROM copilot.audit_logs AS a
        ORDER BY a.seq_no ASC NULLS FIRST, a.audit_id ASC
    LOOP
        v_expected_seq := v_expected_seq + 1;
        v_checked := v_checked + 1;

        IF r.seq_no IS DISTINCT FROM v_expected_seq
           OR r.prev_hash IS DISTINCT FROM v_expected_prev THEN
            RETURN QUERY SELECT false, v_checked, COALESCE(r.seq_no, v_expected_seq);
            RETURN;
        END IF;

        v_canonical := concat_ws(
            '|',
            r.company_id,
            r.seq_no::text,
            r.trace_id,
            COALESCE(r.thread_id, ''),
            r.actor_id,
            r.actor_role,
            r.event_type,
            COALESCE(r.payload_json, '{}'::jsonb)::text,
            to_char(r.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
        );
        v_calc_hash := pg_catalog.sha256(r.prev_hash || convert_to(v_canonical, 'UTF8'));

        IF r.row_hash IS DISTINCT FROM v_calc_hash THEN
            RETURN QUERY SELECT false, v_checked, r.seq_no;
            RETURN;
        END IF;

        v_expected_prev := r.row_hash;
    END LOOP;

    IF v_head_seq IS NULL THEN
        IF v_checked = 0 THEN
            RETURN QUERY SELECT true, 0::BIGINT, NULL::BIGINT;
        ELSE
            RETURN QUERY SELECT false, v_checked, 1::BIGINT;
        END IF;
        RETURN;
    END IF;

    IF v_expected_seq <> v_head_seq OR v_expected_prev IS DISTINCT FROM v_head_hash THEN
        RETURN QUERY SELECT false, v_checked, LEAST(v_expected_seq + 1, v_head_seq);
        RETURN;
    END IF;

    RETURN QUERY SELECT true, v_checked, NULL::BIGINT;
END;
$$;

REVOKE ALL ON FUNCTION copilot.verify_audit_chain()
    FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;
GRANT EXECUTE ON FUNCTION copilot.verify_audit_chain() TO copilot_app;

-- ---------------------------------------------------------------------------
-- (f) Turns Table Metadata Columns
-- ---------------------------------------------------------------------------

ALTER TABLE copilot.turns
    ADD COLUMN IF NOT EXISTS model_id       TEXT,
    ADD COLUMN IF NOT EXISTS prompt_version TEXT,
    ADD COLUMN IF NOT EXISTS input_hash     TEXT,
    ADD COLUMN IF NOT EXISTS answer_hash    TEXT,
    ADD COLUMN IF NOT EXISTS cost_usd_est   NUMERIC(12, 6),
    ADD COLUMN IF NOT EXISTS feedback       TEXT,
    ADD COLUMN IF NOT EXISTS redacted_at    TIMESTAMPTZ;

DO $$
BEGIN
    ALTER TABLE copilot.turns DROP CONSTRAINT IF EXISTS turns_feedback_chk;
    ALTER TABLE copilot.turns
        ADD CONSTRAINT turns_feedback_chk
        CHECK (feedback IS NULL OR feedback IN ('up', 'down'));
END $$;

-- ---------------------------------------------------------------------------
-- (g) Threads Run Lease & Turn Redaction Support
-- ---------------------------------------------------------------------------

ALTER TABLE copilot.threads
    ADD COLUMN IF NOT EXISTS run_lease_until TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_copilot_threads_tenant_actor_updated
    ON copilot.threads (company_id, actor_id, updated_at DESC);

ALTER TABLE copilot.turns
    ALTER COLUMN user_message DROP NOT NULL;
