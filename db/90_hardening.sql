-- 90_hardening.sql
-- Revoke direct pg_catalog.set_config() from application roles so tenants can only be
-- bound once per transaction via telemetry.lock_tenant() / copilot.lock_tenant().

DO $$
BEGIN
    IF current_setting('is_superuser') = 'on' THEN
        REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) FROM PUBLIC;
        REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean)
            FROM telemetry_ro, copilot_app, telemetry_writer;
    ELSE
        RAISE NOTICE 'not superuser: set_config not revoked; tenant lock NOT hardened on this host';
    END IF;
END $$;
