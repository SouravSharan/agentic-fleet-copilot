-- 06_demo_and_integrations.sql
-- Phase D1 operational tables and hardened SECURITY DEFINER helpers:
--   (a) copilot.spend_ledger + aggregate/write-only SECURITY DEFINER accessors

-- ---------------------------------------------------------------------------
-- (a) Global Demo Spend Ledger (NO direct app-role grants; SECURITY DEFINER only)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS copilot.spend_ledger (
    id          BIGSERIAL PRIMARY KEY,
    company_id  TEXT NOT NULL CHECK (company_id <> '*'),
    actor_id    TEXT NOT NULL,
    rev_id      TEXT NOT NULL,
    turn_id     TEXT NOT NULL,
    cost_usd    NUMERIC(12, 6) NOT NULL CHECK (cost_usd >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_copilot_spend_ledger_rev_created
    ON copilot.spend_ledger (rev_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_copilot_spend_ledger_actor_created
    ON copilot.spend_ledger (company_id, actor_id, created_at DESC);

ALTER TABLE copilot.spend_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE copilot.spend_ledger NO FORCE ROW LEVEL SECURITY;

REVOKE ALL ON copilot.spend_ledger
    FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;
REVOKE ALL ON SEQUENCE copilot.spend_ledger_id_seq
    FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;

CREATE OR REPLACE FUNCTION copilot.record_spend(
    p_actor_id   TEXT,
    p_rev_id     TEXT,
    p_turn_id    TEXT,
    p_cost_usd   NUMERIC,
    p_created_at TIMESTAMPTZ DEFAULT now()
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, copilot
AS $$
DECLARE
    v_tenant TEXT;
BEGIN
    v_tenant := copilot.current_tenant();
    IF v_tenant IS NULL THEN
        RAISE EXCEPTION 'tenant context not locked' USING ERRCODE = '42501';
    END IF;
    IF p_cost_usd IS NULL OR p_cost_usd < 0 THEN
        RAISE EXCEPTION 'cost_usd must be non-negative' USING ERRCODE = '22003';
    END IF;

    INSERT INTO copilot.spend_ledger (
        company_id, actor_id, rev_id, turn_id, cost_usd, created_at
    )
    VALUES (
        v_tenant,
        p_actor_id,
        p_rev_id,
        p_turn_id,
        p_cost_usd,
        COALESCE(p_created_at, now())
    );
END;
$$;

CREATE OR REPLACE FUNCTION copilot.spend_total_usd()
RETURNS NUMERIC
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, copilot
AS $$
    SELECT COALESCE(SUM(cost_usd), 0)::numeric FROM copilot.spend_ledger;
$$;

CREATE OR REPLACE FUNCTION copilot.turns_by_rev_since(
    p_rev_id TEXT,
    p_since  TIMESTAMPTZ DEFAULT NULL
)
RETURNS BIGINT
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, copilot
AS $$
    SELECT COUNT(*)::bigint
    FROM copilot.spend_ledger
    WHERE rev_id = p_rev_id
      AND (p_since IS NULL OR created_at >= p_since);
$$;

CREATE OR REPLACE FUNCTION copilot.turns_by_actor_since(
    p_actor_id TEXT,
    p_since    TIMESTAMPTZ
)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, copilot
AS $$
DECLARE
    v_tenant TEXT;
    v_count  BIGINT;
BEGIN
    v_tenant := copilot.current_tenant();
    IF v_tenant IS NULL THEN
        RETURN 0;
    END IF;
    SELECT COUNT(*)::bigint
      INTO v_count
      FROM copilot.spend_ledger
     WHERE company_id = v_tenant
       AND actor_id = p_actor_id
       AND created_at >= p_since;
    RETURN COALESCE(v_count, 0);
END;
$$;

REVOKE ALL ON FUNCTION copilot.record_spend(TEXT, TEXT, TEXT, NUMERIC, TIMESTAMPTZ)
    FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;
REVOKE ALL ON FUNCTION copilot.spend_total_usd()
    FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;
REVOKE ALL ON FUNCTION copilot.turns_by_rev_since(TEXT, TIMESTAMPTZ)
    FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;
REVOKE ALL ON FUNCTION copilot.turns_by_actor_since(TEXT, TIMESTAMPTZ)
    FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;

GRANT EXECUTE ON FUNCTION copilot.record_spend(TEXT, TEXT, TEXT, NUMERIC, TIMESTAMPTZ)
    TO copilot_app;
GRANT EXECUTE ON FUNCTION copilot.spend_total_usd()
    TO copilot_app;
GRANT EXECUTE ON FUNCTION copilot.turns_by_rev_since(TEXT, TIMESTAMPTZ)
    TO copilot_app;
GRANT EXECUTE ON FUNCTION copilot.turns_by_actor_since(TEXT, TIMESTAMPTZ)
    TO copilot_app;
