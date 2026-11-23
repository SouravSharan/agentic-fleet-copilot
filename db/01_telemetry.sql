-- 01_telemetry.sql
-- External platform telemetry schema (read-only to Fleet Copilot via telemetry_ro + RLS).
-- Only the 5 guaranteed snapshot fields are typed columns; all optional blocks remain in JSONB.

CREATE SCHEMA IF NOT EXISTS telemetry;
REVOKE ALL ON SCHEMA telemetry FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Base Tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS telemetry.companies (
    company_id   TEXT PRIMARY KEY CHECK (company_id <> '*'),
    display_name TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS telemetry.telemetry_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    snapshot_id   TEXT UNIQUE NOT NULL,
    company_id    TEXT NOT NULL REFERENCES telemetry.companies (company_id),
    device_id     TEXT NOT NULL,
    employee_id   TEXT NOT NULL,
    collected_at  TIMESTAMPTZ NOT NULL,
    agent_version TEXT NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload       JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object')
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'companies_not_wildcard_chk'
          AND conrelid = 'telemetry.companies'::regclass
    ) THEN
        ALTER TABLE telemetry.companies
            ADD CONSTRAINT companies_not_wildcard_chk CHECK (company_id <> '*');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'snapshots_payload_object_chk'
          AND conrelid = 'telemetry.telemetry_snapshots'::regclass
    ) THEN
        ALTER TABLE telemetry.telemetry_snapshots
            ADD CONSTRAINT snapshots_payload_object_chk CHECK (jsonb_typeof(payload) = 'object');
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_telemetry_snapshots_tenant_device_time
    ON telemetry.telemetry_snapshots (company_id, device_id, collected_at DESC, ingested_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_telemetry_snapshots_tenant_time
    ON telemetry.telemetry_snapshots (company_id, collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_telemetry_snapshots_tenant_employee
    ON telemetry.telemetry_snapshots (company_id, employee_id);

-- ---------------------------------------------------------------------------
-- Safe JSONB Extraction Helpers (drifted / malformed payloads never crash views)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION telemetry.safe_jsonb_text(obj JSONB, key TEXT)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT CASE
        WHEN jsonb_typeof(obj) = 'object'
         AND jsonb_typeof(obj -> key) IN ('string', 'number', 'boolean')
        THEN NULLIF(obj ->> key, '')
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION telemetry.safe_jsonb_bigint(obj JSONB, key TEXT)
RETURNS BIGINT
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    raw JSONB;
BEGIN
    IF jsonb_typeof(obj) <> 'object' THEN
        RETURN NULL;
    END IF;
    raw := obj -> key;
    IF jsonb_typeof(raw) = 'number' THEN
        RETURN trunc((raw #>> '{}')::NUMERIC)::BIGINT;
    ELSIF jsonb_typeof(raw) = 'string' AND NULLIF(btrim(raw #>> '{}'), '') IS NOT NULL THEN
        RETURN trunc((btrim(raw #>> '{}'))::NUMERIC)::BIGINT;
    END IF;
    RETURN NULL;
EXCEPTION
    WHEN OTHERS THEN
        RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION telemetry.safe_jsonb_int(obj JSONB, key TEXT)
RETURNS INTEGER
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    val BIGINT;
BEGIN
    val := telemetry.safe_jsonb_bigint(obj, key);
    IF val IS NULL OR val < -2147483648 OR val > 2147483647 THEN
        RETURN NULL;
    END IF;
    RETURN val::INTEGER;
END;
$$;

CREATE OR REPLACE FUNCTION telemetry.safe_jsonb_bool(obj JSONB, key TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    raw JSONB;
    txt TEXT;
BEGIN
    IF jsonb_typeof(obj) <> 'object' THEN
        RETURN NULL;
    END IF;
    raw := obj -> key;
    IF jsonb_typeof(raw) = 'boolean' THEN
        RETURN (raw #>> '{}')::BOOLEAN;
    ELSIF jsonb_typeof(raw) = 'string' THEN
        txt := lower(btrim(raw #>> '{}'));
        IF txt IN ('true', 'false') THEN
            RETURN txt::BOOLEAN;
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

-- ---------------------------------------------------------------------------
-- Fail-Closed Row-Level Security (enforced on all non-owner NOBYPASSRLS roles)
-- ---------------------------------------------------------------------------

ALTER TABLE telemetry.companies ENABLE ROW LEVEL SECURITY;
ALTER TABLE telemetry.companies NO FORCE ROW LEVEL SECURITY;

ALTER TABLE telemetry.telemetry_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE telemetry.telemetry_snapshots NO FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS companies_tenant_isolation_ro ON telemetry.companies;
CREATE POLICY companies_tenant_isolation_ro ON telemetry.companies
    FOR SELECT
    TO telemetry_ro
    USING (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
    );

DROP POLICY IF EXISTS companies_writer_all ON telemetry.companies;
CREATE POLICY companies_writer_all ON telemetry.companies
    FOR ALL
    TO telemetry_writer
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS snapshots_tenant_isolation_ro ON telemetry.telemetry_snapshots;
CREATE POLICY snapshots_tenant_isolation_ro ON telemetry.telemetry_snapshots
    FOR SELECT
    TO telemetry_ro
    USING (
        NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
        AND company_id = current_setting('app.current_tenant', true)
    );

DROP POLICY IF EXISTS snapshots_writer_all ON telemetry.telemetry_snapshots;
CREATE POLICY snapshots_writer_all ON telemetry.telemetry_snapshots
    FOR ALL
    TO telemetry_writer
    USING (true)
    WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- Live JSONB Views (security_invoker + security_barrier enforce telemetry_ro RLS)
-- ---------------------------------------------------------------------------

-- 1. Deduplicated event-time snapshot stream with safe, NULL-tolerant extraction.
CREATE OR REPLACE VIEW telemetry.v_snapshots_deduped
WITH (security_invoker = true, security_barrier = true) AS
SELECT DISTINCT ON (s.company_id, s.device_id, s.collected_at)
    s.id,
    s.snapshot_id,
    s.company_id,
    s.device_id,
    s.employee_id,
    s.collected_at,
    s.agent_version,
    s.ingested_at,
    s.payload,
    -- OS fields (nullable)
    telemetry.safe_jsonb_text(s.payload->'os', 'platform')        AS os_platform,
    telemetry.safe_jsonb_text(s.payload->'os', 'product_name')    AS os_product_name,
    telemetry.safe_jsonb_text(s.payload->'os', 'product_version') AS os_product_version,
    telemetry.safe_jsonb_text(s.payload->'os', 'build_version')   AS os_build_version,
    telemetry.safe_jsonb_text(s.payload->'os', 'architecture')    AS os_architecture,
    telemetry.safe_jsonb_text(s.payload->'os', 'hostname')        AS os_hostname,
    -- Device identity fields (nullable)
    telemetry.safe_jsonb_text(s.payload->'device_identity', 'serial_number')    AS serial_number,
    telemetry.safe_jsonb_text(s.payload->'device_identity', 'model_name')       AS model_name,
    telemetry.safe_jsonb_text(s.payload->'device_identity', 'model_identifier') AS model_identifier,
    telemetry.safe_jsonb_text(s.payload->'device_identity', 'processor')        AS processor,
    telemetry.safe_jsonb_text(s.payload->'device_identity', 'hardware_uuid')    AS hardware_uuid,
    telemetry.safe_jsonb_text(s.payload->'device_identity', 'total_memory')     AS total_memory_label,
    -- Memory fields (nullable; denominator must be strictly > 0)
    telemetry.safe_jsonb_bigint(s.payload->'memory', 'total_memory_bytes') AS ram_total_bytes,
    telemetry.safe_jsonb_bigint(s.payload->'memory', 'used_memory_bytes')  AS ram_used_bytes,
    telemetry.safe_jsonb_bigint(s.payload->'memory', 'free_memory_bytes')  AS ram_free_bytes,
    CASE
        WHEN telemetry.safe_jsonb_bigint(s.payload->'memory', 'total_memory_bytes') > 0
         AND telemetry.safe_jsonb_bigint(s.payload->'memory', 'used_memory_bytes') >= 0
        THEN ROUND(
            (telemetry.safe_jsonb_bigint(s.payload->'memory', 'used_memory_bytes')::NUMERIC
             / telemetry.safe_jsonb_bigint(s.payload->'memory', 'total_memory_bytes')::NUMERIC) * 100.0,
            2
        )
        ELSE NULL
    END AS ram_used_pct,
    -- Battery fields (nullable)
    COALESCE(jsonb_typeof(s.payload->'battery') = 'object', false)          AS has_battery_block,
    telemetry.safe_jsonb_bool(s.payload->'battery', 'battery_present')      AS battery_present,
    telemetry.safe_jsonb_text(s.payload->'battery', 'charging_status')      AS battery_charging_status,
    telemetry.safe_jsonb_int(s.payload->'battery', 'percentage')            AS battery_percentage,
    telemetry.safe_jsonb_text(s.payload->'battery', 'condition')            AS battery_condition,
    telemetry.safe_jsonb_int(s.payload->'battery', 'cycle_count')           AS battery_cycle_count,
    telemetry.safe_jsonb_int(s.payload->'battery', 'full_charge_capacity')  AS battery_full_charge_capacity,
    -- Network block presence (CASE prevents non-short-circuiting AND evaluation)
    CASE
        WHEN jsonb_typeof(s.payload->'network') = 'array'
        THEN jsonb_array_length(s.payload->'network') > 0
        ELSE false
    END AS has_network_block
FROM telemetry.telemetry_snapshots AS s
ORDER BY s.company_id, s.device_id, s.collected_at, s.ingested_at DESC, s.id DESC;

-- 2. Latest device state ordered by event time (collected_at), resilient to partial payloads.
CREATE OR REPLACE VIEW telemetry.v_latest_device_state
WITH (security_invoker = true, security_barrier = true) AS
WITH device_history AS (
    SELECT
        company_id,
        device_id,
        MIN(collected_at) AS first_seen_at,
        MAX(collected_at) AS last_seen_at,
        COUNT(*)::INTEGER AS snapshot_count,
        BOOL_OR(COALESCE(battery_present, false)) AS ever_had_battery,
        (ARRAY_AGG(model_name ORDER BY collected_at DESC)
            FILTER (WHERE model_name IS NOT NULL))[1] AS last_known_model_name,
        (ARRAY_AGG(serial_number ORDER BY collected_at DESC)
            FILTER (WHERE serial_number IS NOT NULL))[1] AS last_known_serial_number,
        (ARRAY_AGG(os_platform ORDER BY collected_at DESC)
            FILTER (WHERE os_platform IS NOT NULL))[1] AS last_known_os_platform,
        (ARRAY_AGG(os_product_name ORDER BY collected_at DESC)
            FILTER (WHERE os_product_name IS NOT NULL))[1] AS last_known_os_name,
        (ARRAY_AGG(os_product_version ORDER BY collected_at DESC)
            FILTER (WHERE os_product_version IS NOT NULL))[1] AS last_known_os_version,
        (ARRAY_AGG(os_hostname ORDER BY collected_at DESC)
            FILTER (WHERE os_hostname IS NOT NULL))[1] AS last_known_hostname
    FROM telemetry.v_snapshots_deduped
    GROUP BY company_id, device_id
),
latest_snap AS (
    SELECT DISTINCT ON (company_id, device_id)
        *
    FROM telemetry.v_snapshots_deduped
    ORDER BY company_id, device_id, collected_at DESC
)
SELECT
    l.snapshot_id,
    l.company_id,
    l.device_id,
    l.employee_id,
    l.collected_at,
    l.agent_version,
    l.ingested_at,
    COALESCE(l.os_platform, h.last_known_os_platform)       AS os_platform,
    COALESCE(l.os_product_name, h.last_known_os_name)       AS os_product_name,
    COALESCE(l.os_product_version, h.last_known_os_version) AS os_product_version,
    l.os_build_version,
    l.os_architecture,
    COALESCE(l.os_hostname, h.last_known_hostname)          AS os_hostname,
    COALESCE(l.serial_number, h.last_known_serial_number)   AS serial_number,
    COALESCE(l.model_name, h.last_known_model_name)         AS model_name,
    l.model_identifier,
    l.processor,
    l.hardware_uuid,
    l.total_memory_label,
    l.ram_total_bytes,
    l.ram_used_bytes,
    l.ram_free_bytes,
    l.ram_used_pct,
    l.has_battery_block,
    l.battery_present,
    l.battery_charging_status,
    l.battery_percentage,
    l.battery_condition,
    l.battery_cycle_count,
    l.battery_full_charge_capacity,
    l.has_network_block,
    h.first_seen_at,
    h.last_seen_at,
    h.snapshot_count,
    h.ever_had_battery,
    -- Check battery evidence & laptop models BEFORE desktop patterns so battery-powered
    -- "Laptop Studio" / "Mobile Workstation" models are never misclassified as desktops.
    CASE
        WHEN h.ever_had_battery THEN 'laptop'
        WHEN COALESCE(l.model_name, h.last_known_model_name, '') ~* '\m(macbook|laptop|thinkpad|xps|chromebook|elitebook|latitude|zenbook)\M'
        THEN 'laptop'
        WHEN COALESCE(l.model_name, h.last_known_model_name, '') ~* '\m(mac mini|imac|mac studio|desktop|tower|workstation)\M'
        THEN 'desktop'
        ELSE 'unknown'
    END AS form_factor
FROM latest_snap AS l
JOIN device_history AS h
  ON h.company_id = l.company_id
 AND h.device_id  = l.device_id;

-- 3. Flattened disk volumes (safe for missing, JSON-null, or malformed blocks).
CREATE OR REPLACE VIEW telemetry.v_disk_volumes
WITH (security_invoker = true, security_barrier = true) AS
SELECT
    s.snapshot_id,
    s.company_id,
    s.device_id,
    s.employee_id,
    s.collected_at,
    telemetry.safe_jsonb_text(vol, 'volume_name')   AS volume_name,
    telemetry.safe_jsonb_text(vol, 'file_system')   AS file_system,
    telemetry.safe_jsonb_text(vol, 'mount_point')   AS mount_point,
    telemetry.safe_jsonb_bigint(vol, 'size_bytes')      AS size_bytes,
    telemetry.safe_jsonb_bigint(vol, 'available_bytes') AS available_bytes,
    CASE
        WHEN telemetry.safe_jsonb_bigint(vol, 'size_bytes') > 0
         AND telemetry.safe_jsonb_bigint(vol, 'available_bytes') >= 0
        THEN ROUND(
            (telemetry.safe_jsonb_bigint(vol, 'available_bytes')::NUMERIC
             / telemetry.safe_jsonb_bigint(vol, 'size_bytes')::NUMERIC) * 100.0,
            2
        )
        ELSE NULL
    END AS free_pct,
    telemetry.safe_jsonb_bool(vol, 'encrypted')     AS encrypted
FROM telemetry.v_snapshots_deduped AS s
CROSS JOIN LATERAL jsonb_array_elements(
    CASE
        WHEN jsonb_typeof(s.payload->'disk_volumes') = 'array' THEN s.payload->'disk_volumes'
        ELSE '[]'::jsonb
    END
) AS vol
WHERE jsonb_typeof(vol) = 'object';

-- 4. Flattened compliance results.
CREATE OR REPLACE VIEW telemetry.v_compliance_results
WITH (security_invoker = true, security_barrier = true) AS
SELECT
    s.snapshot_id,
    s.company_id,
    s.device_id,
    s.employee_id,
    s.collected_at,
    telemetry.safe_jsonb_text(chk, 'check_id') AS check_id,
    telemetry.safe_jsonb_text(chk, 'status')   AS status,
    telemetry.safe_jsonb_text(chk, 'severity') AS severity
FROM telemetry.v_snapshots_deduped AS s
CROSS JOIN LATERAL jsonb_array_elements(
    CASE
        WHEN jsonb_typeof(s.payload->'compliance_results') = 'array' THEN s.payload->'compliance_results'
        ELSE '[]'::jsonb
    END
) AS chk
WHERE jsonb_typeof(chk) = 'object';

-- 5. Flattened installed software.
CREATE OR REPLACE VIEW telemetry.v_installed_software
WITH (security_invoker = true, security_barrier = true) AS
SELECT
    s.snapshot_id,
    s.company_id,
    s.device_id,
    s.employee_id,
    s.collected_at,
    telemetry.safe_jsonb_text(sw, 'name')      AS software_name,
    telemetry.safe_jsonb_text(sw, 'version')   AS version,
    telemetry.safe_jsonb_text(sw, 'publisher') AS publisher
FROM telemetry.v_snapshots_deduped AS s
CROSS JOIN LATERAL jsonb_array_elements(
    CASE
        WHEN jsonb_typeof(s.payload->'installed_software') = 'array' THEN s.payload->'installed_software'
        ELSE '[]'::jsonb
    END
) AS sw
WHERE jsonb_typeof(sw) = 'object';

-- 6. Flattened network interfaces.
CREATE OR REPLACE VIEW telemetry.v_network_interfaces
WITH (security_invoker = true, security_barrier = true) AS
SELECT
    s.snapshot_id,
    s.company_id,
    s.device_id,
    s.employee_id,
    s.collected_at,
    telemetry.safe_jsonb_text(net, 'interface_name') AS interface_name,
    telemetry.safe_jsonb_text(net, 'address')        AS address,
    telemetry.safe_jsonb_text(net, 'family')         AS family,
    telemetry.safe_jsonb_text(net, 'mac')            AS mac,
    telemetry.safe_jsonb_bool(net, 'internal')       AS internal
FROM telemetry.v_snapshots_deduped AS s
CROSS JOIN LATERAL jsonb_array_elements(
    CASE
        WHEN jsonb_typeof(s.payload->'network') = 'array' THEN s.payload->'network'
        ELSE '[]'::jsonb
    END
) AS net
WHERE jsonb_typeof(net) = 'object';

-- ---------------------------------------------------------------------------
-- Tenant Lock Function (write-once transaction-local tenant binding)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION telemetry.lock_tenant(p_company_id TEXT)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF p_company_id IS NULL OR p_company_id !~ '^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$' THEN
        RAISE EXCEPTION 'invalid company_id' USING ERRCODE = '22023';
    END IF;
    IF current_setting('app.tenant_locked', true) = 'true' THEN
        RAISE EXCEPTION 'tenant already locked in this transaction' USING ERRCODE = '42501';
    END IF;
    PERFORM set_config('app.current_tenant', p_company_id, true);
    PERFORM set_config('app.tenant_locked', 'true', true);
END;
$$;

REVOKE ALL ON FUNCTION telemetry.lock_tenant(TEXT)
    FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;
GRANT EXECUTE ON FUNCTION telemetry.lock_tenant(TEXT) TO telemetry_ro;

-- ---------------------------------------------------------------------------
-- Least-Privilege Grants
-- ---------------------------------------------------------------------------

REVOKE ALL ON
    telemetry.companies,
    telemetry.telemetry_snapshots,
    telemetry.v_snapshots_deduped,
    telemetry.v_latest_device_state,
    telemetry.v_disk_volumes,
    telemetry.v_compliance_results,
    telemetry.v_installed_software,
    telemetry.v_network_interfaces
FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;

GRANT USAGE ON SCHEMA telemetry TO telemetry_ro, telemetry_writer;

GRANT EXECUTE ON FUNCTION
    telemetry.safe_jsonb_text(JSONB, TEXT),
    telemetry.safe_jsonb_bigint(JSONB, TEXT),
    telemetry.safe_jsonb_int(JSONB, TEXT),
    telemetry.safe_jsonb_bool(JSONB, TEXT)
TO telemetry_ro, telemetry_writer;

-- telemetry_ro: SELECT only (underlying table SELECT is required by security_invoker views; RLS filters both).
GRANT SELECT ON
    telemetry.companies,
    telemetry.telemetry_snapshots,
    telemetry.v_snapshots_deduped,
    telemetry.v_latest_device_state,
    telemetry.v_disk_volumes,
    telemetry.v_compliance_results,
    telemetry.v_installed_software,
    telemetry.v_network_interfaces
TO telemetry_ro;

-- telemetry_writer: ingestion & simulation only.
GRANT SELECT, INSERT, UPDATE, TRUNCATE ON
    telemetry.companies,
    telemetry.telemetry_snapshots
TO telemetry_writer;

GRANT USAGE, SELECT ON SEQUENCE telemetry.telemetry_snapshots_id_seq TO telemetry_writer;
