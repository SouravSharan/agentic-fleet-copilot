-- 00_roles.sql
-- Idempotently provisions least-privilege database roles (no hardcoded passwords).
-- Passwords are applied by scripts/db_setup.py from environment variables.
-- Safe on both local/VPS superuser Postgres and managed Postgres (e.g. Supabase).

DO $$
DECLARE
    role_name TEXT;
    is_super  BOOLEAN := current_setting('is_superuser') = 'on';
BEGIN
    FOREACH role_name IN ARRAY ARRAY['telemetry_writer', 'telemetry_ro', 'copilot_app', 'demo_ops'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            -- Newly created roles default to NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS.
            EXECUTE format('CREATE ROLE %I LOGIN', role_name);
        END IF;

        IF is_super THEN
            EXECUTE format(
                'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
                role_name
            );
        END IF;
    END LOOP;

    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO telemetry_writer, telemetry_ro, copilot_app, demo_ops',
        current_database()
    );

    -- Defense-in-depth: telemetry_ro defaults to read-only transactions at the role level.
    EXECUTE 'ALTER ROLE telemetry_ro SET default_transaction_read_only = on';
    EXECUTE 'ALTER ROLE telemetry_ro SET search_path TO telemetry';
    EXECUTE 'ALTER ROLE telemetry_writer SET search_path TO telemetry';
END
$$;

-- Prevent unprivileged roles from creating stray objects in public (best-effort on managed PG).
DO $$
BEGIN
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
EXCEPTION
    WHEN insufficient_privilege THEN
        NULL;
END
$$;
