-- 90_hardening.sql
-- Belt-and-suspenders hardening:
-- 1. Revoke direct pg_catalog.set_config() from application roles.
-- 2. Re-assert owner-only permissions on signed-tenant secret tables.

REVOKE ALL ON telemetry.tenant_lock_secret
    FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;
REVOKE ALL ON copilot.tenant_lock_secret
    FROM PUBLIC, telemetry_ro, telemetry_writer, copilot_app;

DO $$
BEGIN
    IF current_setting('is_superuser') = 'on' THEN
        REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) FROM PUBLIC;
        REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean)
            FROM telemetry_ro, copilot_app, telemetry_writer;
    ELSE
        RAISE NOTICE 'not superuser: set_config not revoked; rely on signed current_tenant()';
    END IF;
END $$;
