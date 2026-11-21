-- 02_copilot.sql
-- Fleet Copilot owned operational schema (read/write via copilot_app; append-only audit_logs).

CREATE SCHEMA IF NOT EXISTS copilot;
REVOKE ALL ON SCHEMA copilot FROM PUBLIC;

-- Route LangGraph AsyncPostgresSaver tables automatically into copilot schema.
ALTER ROLE copilot_app SET search_path TO copilot;
GRANT USAGE, CREATE ON SCHEMA copilot TO copilot_app;

-- ---------------------------------------------------------------------------
-- Operational Tables (tenant-scoped composite FKs prevent cross-tenant linkage)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS copilot.threads (
    thread_id  TEXT PRIMARY KEY,
    company_id TEXT NOT NULL CHECK (company_id <> '*'),
    actor_id   TEXT NOT NULL,
    title      TEXT,
    status     TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'waiting_approval', 'completed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_copilot_threads_tenant_updated
    ON copilot.threads (company_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS copilot.turns (
    turn_id            TEXT PRIMARY KEY,
    trace_id           TEXT NOT NULL,
    thread_id          TEXT NOT NULL,
    company_id         TEXT NOT NULL CHECK (company_id <> '*'),
    actor_id           TEXT NOT NULL,
    user_message       TEXT NOT NULL,
    plan_json          JSONB,
    tool_calls_json    JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_json      JSONB NOT NULL DEFAULT '[]'::jsonb,
    assistant_response TEXT,
    status             TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'waiting_approval', 'completed', 'refused', 'failed')),
    llm_model          TEXT,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    latency_ms         INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at       TIMESTAMPTZ,
    UNIQUE (company_id, thread_id, turn_id),
    CONSTRAINT turns_tenant_thread_fk
        FOREIGN KEY (company_id, thread_id)
        REFERENCES copilot.threads (company_id, thread_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_copilot_turns_thread_created
    ON copilot.turns (thread_id, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_copilot_turns_tenant_trace
    ON copilot.turns (company_id, trace_id);

CREATE TABLE IF NOT EXISTS copilot.tenant_policies (
    policy_id   BIGSERIAL PRIMARY KEY,
    company_id  TEXT NOT NULL,
    scope_type  TEXT NOT NULL
        CHECK (scope_type IN ('global', 'tenant', 'device_type', 'device')),
    scope_value TEXT NOT NULL DEFAULT '',
    metric_key  TEXT NOT NULL,
    value_json  JSONB NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT 'system',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, scope_type, scope_value, metric_key),
    CONSTRAINT policies_global_scope_consistency_chk
        CHECK ((company_id = '*') = (scope_type = 'global'))
);

CREATE INDEX IF NOT EXISTS idx_copilot_policies_lookup
    ON copilot.tenant_policies (company_id, metric_key);

CREATE TABLE IF NOT EXISTS copilot.tenant_tool_configs (
    company_id          TEXT NOT NULL,
    tool_name           TEXT NOT NULL,
    enabled             BOOLEAN NOT NULL DEFAULT true,
    requires_approval   BOOLEAN NOT NULL DEFAULT false,
    min_role            TEXT NOT NULL DEFAULT 'viewer'
        CHECK (min_role IN ('employee', 'viewer', 'it_admin')),
    adapter_config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by          TEXT NOT NULL DEFAULT 'system',
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, tool_name)
);

CREATE TABLE IF NOT EXISTS copilot.action_proposals (
    proposal_id           TEXT PRIMARY KEY,
    batch_id              TEXT NOT NULL,
    thread_id             TEXT NOT NULL,
    turn_id               TEXT NOT NULL,
    company_id            TEXT NOT NULL CHECK (company_id <> '*'),
    proposed_by_actor_id  TEXT NOT NULL,
    tool_name             TEXT NOT NULL,
    target_type           TEXT NOT NULL CHECK (target_type IN ('device', 'employee')),
    target_id             TEXT NOT NULL,
    arguments_json        JSONB NOT NULL,
    rationale             TEXT NOT NULL,
    evidence_snapshot_ids JSONB NOT NULL,
    evidence_as_of        TIMESTAMPTZ NOT NULL,
    policy_check_json     JSONB NOT NULL,
    status                TEXT NOT NULL
        CHECK (status IN (
            'pending_approval',
            'approved',
            'rejected',
            'refused_by_guardrail',
            'stale_blocked',
            'executed',
            'failed'
        )),
    reviewer_id           TEXT,
    review_note           TEXT,
    reviewed_at           TIMESTAMPTZ,
    external_receipt_json JSONB,
    executed_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT proposals_tenant_turn_fk
        FOREIGN KEY (company_id, thread_id, turn_id)
        REFERENCES copilot.turns (company_id, thread_id, turn_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_copilot_proposals_tenant_status
    ON copilot.action_proposals (company_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_copilot_proposals_dedupe
    ON copilot.action_proposals (company_id, target_id, tool_name, status);

CREATE TABLE IF NOT EXISTS copilot.audit_logs (
    audit_id     BIGSERIAL PRIMARY KEY,
    trace_id     TEXT NOT NULL,
    thread_id    TEXT,
    company_id   TEXT NOT NULL CHECK (company_id <> '*'),
    actor_id     TEXT NOT NULL,
    actor_role   TEXT NOT NULL
        CHECK (actor_role IN ('employee', 'viewer', 'it_admin', 'platform_admin', 'service')),
    event_type   TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_copilot_audit_tenant_created
    ON copilot.audit_logs (company_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_copilot_audit_trace
    ON copilot.audit_logs (trace_id);

-- ---------------------------------------------------------------------------
-- Idempotent Constraint Upgrades (for already-initialized local DBs)
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    -- Composite unique keys for tenant-scoped FKs
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'copilot.threads'::regclass
          AND contype = 'u'
          AND pg_get_constraintdef(oid) = 'UNIQUE (company_id, thread_id)'
    ) THEN
        ALTER TABLE copilot.threads
            ADD CONSTRAINT threads_company_thread_uniq UNIQUE (company_id, thread_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'copilot.turns'::regclass
          AND contype = 'u'
          AND pg_get_constraintdef(oid) = 'UNIQUE (company_id, thread_id, turn_id)'
    ) THEN
        ALTER TABLE copilot.turns
            ADD CONSTRAINT turns_company_thread_turn_uniq UNIQUE (company_id, thread_id, turn_id);
    END IF;

    -- Replace legacy single-column FKs if present
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'copilot.turns'::regclass AND conname = 'turns_thread_id_fkey'
    ) THEN
        ALTER TABLE copilot.turns DROP CONSTRAINT turns_thread_id_fkey;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'copilot.turns'::regclass AND conname = 'turns_tenant_thread_fk'
    ) THEN
        ALTER TABLE copilot.turns
            ADD CONSTRAINT turns_tenant_thread_fk
            FOREIGN KEY (company_id, thread_id)
            REFERENCES copilot.threads (company_id, thread_id)
            ON DELETE CASCADE;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'copilot.action_proposals'::regclass
          AND conname = 'action_proposals_thread_id_fkey'
    ) THEN
        ALTER TABLE copilot.action_proposals DROP CONSTRAINT action_proposals_thread_id_fkey;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'copilot.action_proposals'::regclass
          AND conname = 'action_proposals_turn_id_fkey'
    ) THEN
        ALTER TABLE copilot.action_proposals DROP CONSTRAINT action_proposals_turn_id_fkey;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'copilot.action_proposals'::regclass
          AND conname = 'proposals_tenant_turn_fk'
    ) THEN
        ALTER TABLE copilot.action_proposals
            ADD CONSTRAINT proposals_tenant_turn_fk
            FOREIGN KEY (company_id, thread_id, turn_id)
            REFERENCES copilot.turns (company_id, thread_id, turn_id)
            ON DELETE CASCADE;
    END IF;

    -- Non-wildcard & scope consistency CHECK constraints
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'copilot.tenant_policies'::regclass
          AND conname = 'policies_global_scope_consistency_chk'
    ) THEN
        ALTER TABLE copilot.tenant_policies
            ADD CONSTRAINT policies_global_scope_consistency_chk
            CHECK ((company_id = '*') = (scope_type = 'global'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'copilot.threads'::regclass AND conname = 'threads_not_wildcard_chk'
    ) THEN
        ALTER TABLE copilot.threads ADD CONSTRAINT threads_not_wildcard_chk CHECK (company_id <> '*');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'copilot.turns'::regclass AND conname = 'turns_not_wildcard_chk'
    ) THEN
        ALTER TABLE copilot.turns ADD CONSTRAINT turns_not_wildcard_chk CHECK (company_id <> '*');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'copilot.action_proposals'::regclass
          AND conname = 'proposals_not_wildcard_chk'
    ) THEN
        ALTER TABLE copilot.action_proposals
            ADD CONSTRAINT proposals_not_wildcard_chk CHECK (company_id <> '*');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'copilot.audit_logs'::regclass AND conname = 'audit_not_wildcard_chk'
    ) THEN
        ALTER TABLE copilot.audit_logs ADD CONSTRAINT audit_not_wildcard_chk CHECK (company_id <> '*');
    END IF;

    -- Refresh role CHECKs so 'employee' is supported without future schema changes
    ALTER TABLE copilot.tenant_tool_configs DROP CONSTRAINT IF EXISTS tenant_tool_configs_min_role_check;
    ALTER TABLE copilot.tenant_tool_configs
        ADD CONSTRAINT tenant_tool_configs_min_role_check
        CHECK (min_role IN ('employee', 'viewer', 'it_admin'));

    ALTER TABLE copilot.audit_logs DROP CONSTRAINT IF EXISTS audit_logs_actor_role_check;
    ALTER TABLE copilot.audit_logs
        ADD CONSTRAINT audit_logs_actor_role_check
        CHECK (actor_role IN ('employee', 'viewer', 'it_admin', 'platform_admin', 'service'));
END
$$;

-- ---------------------------------------------------------------------------
-- Row-Level Security (Fail-Closed on app.current_tenant for non-owner copilot_app)
-- ---------------------------------------------------------------------------

ALTER TABLE copilot.threads             ENABLE ROW LEVEL SECURITY;
ALTER TABLE copilot.threads             NO FORCE ROW LEVEL SECURITY;
ALTER TABLE copilot.turns               ENABLE ROW LEVEL SECURITY;
ALTER TABLE copilot.turns               NO FORCE ROW LEVEL SECURITY;
ALTER TABLE copilot.tenant_policies     ENABLE ROW LEVEL SECURITY;
ALTER TABLE copilot.tenant_policies     NO FORCE ROW LEVEL SECURITY;
ALTER TABLE copilot.tenant_tool_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE copilot.tenant_tool_configs NO FORCE ROW LEVEL SECURITY;
ALTER TABLE copilot.action_proposals    ENABLE ROW LEVEL SECURITY;
ALTER TABLE copilot.action_proposals    NO FORCE ROW LEVEL SECURITY;
ALTER TABLE copilot.audit_logs          ENABLE ROW LEVEL SECURITY;
ALTER TABLE copilot.audit_logs          NO FORCE ROW LEVEL SECURITY;

-- Strict single-tenant operational tables (threads, turns, action_proposals)
DROP POLICY IF EXISTS threads_tenant_isolation ON copilot.threads;
CREATE POLICY threads_tenant_isolation ON copilot.threads
    FOR ALL TO copilot_app
    USING (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
    )
    WITH CHECK (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
    );

DROP POLICY IF EXISTS turns_tenant_isolation ON copilot.turns;
CREATE POLICY turns_tenant_isolation ON copilot.turns
    FOR ALL TO copilot_app
    USING (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
    )
    WITH CHECK (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
    );

DROP POLICY IF EXISTS proposals_tenant_isolation ON copilot.action_proposals;
CREATE POLICY proposals_tenant_isolation ON copilot.action_proposals
    FOR ALL TO copilot_app
    USING (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
    )
    WITH CHECK (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
    );

-- Append-only audit_logs (SELECT + INSERT only; UPDATE/DELETE/TRUNCATE also revoked below)
DROP POLICY IF EXISTS audit_tenant_select ON copilot.audit_logs;
CREATE POLICY audit_tenant_select ON copilot.audit_logs
    FOR SELECT TO copilot_app
    USING (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
    );

DROP POLICY IF EXISTS audit_tenant_insert ON copilot.audit_logs;
CREATE POLICY audit_tenant_insert ON copilot.audit_logs
    FOR INSERT TO copilot_app
    WITH CHECK (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
    );

-- Policies & tool configs: tenants read global '*' + own rows, write ONLY own non-global rows.
DROP POLICY IF EXISTS policies_tenant_select ON copilot.tenant_policies;
CREATE POLICY policies_tenant_select ON copilot.tenant_policies
    FOR SELECT TO copilot_app
    USING (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id IN ('*', current_setting('app.current_tenant', true))
    );

DROP POLICY IF EXISTS policies_tenant_insert ON copilot.tenant_policies;
CREATE POLICY policies_tenant_insert ON copilot.tenant_policies
    FOR INSERT TO copilot_app
    WITH CHECK (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
        AND company_id <> '*'
        AND scope_type <> 'global'
    );

DROP POLICY IF EXISTS policies_tenant_update ON copilot.tenant_policies;
CREATE POLICY policies_tenant_update ON copilot.tenant_policies
    FOR UPDATE TO copilot_app
    USING (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
        AND company_id <> '*'
        AND scope_type <> 'global'
    )
    WITH CHECK (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
        AND company_id <> '*'
        AND scope_type <> 'global'
    );

DROP POLICY IF EXISTS policies_tenant_delete ON copilot.tenant_policies;
CREATE POLICY policies_tenant_delete ON copilot.tenant_policies
    FOR DELETE TO copilot_app
    USING (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
        AND company_id <> '*'
        AND scope_type <> 'global'
    );

DROP POLICY IF EXISTS tool_configs_tenant_select ON copilot.tenant_tool_configs;
CREATE POLICY tool_configs_tenant_select ON copilot.tenant_tool_configs
    FOR SELECT TO copilot_app
    USING (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id IN ('*', current_setting('app.current_tenant', true))
    );

DROP POLICY IF EXISTS tool_configs_tenant_write ON copilot.tenant_tool_configs;
CREATE POLICY tool_configs_tenant_write ON copilot.tenant_tool_configs
    FOR INSERT TO copilot_app
    WITH CHECK (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
        AND company_id <> '*'
    );

DROP POLICY IF EXISTS tool_configs_tenant_update ON copilot.tenant_tool_configs;
CREATE POLICY tool_configs_tenant_update ON copilot.tenant_tool_configs
    FOR UPDATE TO copilot_app
    USING (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
        AND company_id <> '*'
    )
    WITH CHECK (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
        AND company_id <> '*'
    );

DROP POLICY IF EXISTS tool_configs_tenant_delete ON copilot.tenant_tool_configs;
CREATE POLICY tool_configs_tenant_delete ON copilot.tenant_tool_configs
    FOR DELETE TO copilot_app
    USING (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
        AND company_id <> '*'
    );

-- ---------------------------------------------------------------------------
-- Least-Privilege Grants (scoped to our 6 tables; never touches LangGraph checkpoints)
-- ---------------------------------------------------------------------------

REVOKE ALL ON
    copilot.threads,
    copilot.turns,
    copilot.tenant_policies,
    copilot.tenant_tool_configs,
    copilot.action_proposals,
    copilot.audit_logs
FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;

-- Operational history (threads, turns, action_proposals): NO runtime DELETE.
GRANT SELECT, INSERT, UPDATE ON
    copilot.threads,
    copilot.turns,
    copilot.action_proposals
TO copilot_app;

-- Tenant-editable overrides: SELECT, INSERT, UPDATE, DELETE (RLS blocks global '*').
GRANT SELECT, INSERT, UPDATE, DELETE ON
    copilot.tenant_policies,
    copilot.tenant_tool_configs
TO copilot_app;

GRANT USAGE, SELECT ON SEQUENCE copilot.tenant_policies_policy_id_seq TO copilot_app;

-- Append-only: copilot_app may only SELECT and INSERT on audit_logs.
GRANT SELECT, INSERT ON copilot.audit_logs TO copilot_app;
GRANT USAGE, SELECT ON SEQUENCE copilot.audit_logs_audit_id_seq TO copilot_app;

-- ---------------------------------------------------------------------------
-- Global System Defaults (company_id = '*', owned by platform admin)
-- ---------------------------------------------------------------------------

INSERT INTO copilot.tenant_policies (company_id, scope_type, scope_value, metric_key, value_json, updated_by)
VALUES
    ('*', 'global', '', 'low_disk_free_pct',          '15.0'::jsonb, 'system'),
    ('*', 'global', '', 'high_ram_used_pct',          '80.0'::jsonb, 'system'),
    ('*', 'global', '', 'ram_sustained_ratio',        '0.7'::jsonb,  'system'),
    ('*', 'global', '', 'battery_eol_cycles',         '800'::jsonb,  'system'),
    ('*', 'global', '', 'battery_capacity_drop_pct',  '3.0'::jsonb,  'system'),
    ('*', 'global', '', 'stale_device_days',          '7'::jsonb,    'system'),
    ('*', 'global', '', 'flagged_software_list',      '["uTorrent", "TeamViewer", "CleanMyMac X"]'::jsonb, 'system')
ON CONFLICT (company_id, scope_type, scope_value, metric_key) DO NOTHING;

INSERT INTO copilot.tenant_tool_configs (company_id, tool_name, enabled, requires_approval, min_role, adapter_config_json, updated_by)
VALUES
    ('*', 'get_fleet_overview',          true, false, 'viewer',   '{}'::jsonb, 'system'),
    ('*', 'query_devices',               true, false, 'viewer',   '{}'::jsonb, 'system'),
    ('*', 'query_compliance',            true, false, 'viewer',   '{}'::jsonb, 'system'),
    ('*', 'get_device_details',          true, false, 'viewer',   '{}'::jsonb, 'system'),
    ('*', 'get_operational_context',     true, false, 'viewer',   '{}'::jsonb, 'system'),
    ('*', 'analyze_battery_trends',      true, false, 'viewer',   '{}'::jsonb, 'system'),
    ('*', 'analyze_resource_trends',     true, false, 'viewer',   '{}'::jsonb, 'system'),
    ('*', 'analyze_compliance_drift',    true, false, 'viewer',   '{}'::jsonb, 'system'),
    ('*', 'analyze_software_changes',    true, false, 'viewer',   '{}'::jsonb, 'system'),
    ('*', 'create_upgrade_order',        true, true,  'it_admin', '{"mode": "mock"}'::jsonb, 'system'),
    ('*', 'open_remediation_ticket',     true, true,  'it_admin', '{"mode": "mock"}'::jsonb, 'system'),
    ('*', 'flag_device_for_replacement', true, true,  'it_admin', '{"mode": "mock"}'::jsonb, 'system'),
    ('*', 'notify_employee',             true, true,  'it_admin', '{"mode": "mock"}'::jsonb, 'system')
ON CONFLICT (company_id, tool_name) DO NOTHING;
