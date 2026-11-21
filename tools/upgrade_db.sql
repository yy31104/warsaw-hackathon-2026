\set ON_ERROR_STOP on
-- A new volume already ran these files at init. Existing main volumes need
-- the same changes before an API or worker reads the config registry.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('alertfunnel:database-upgrade', 0));
SELECT to_regclass('config_state') IS NULL AS needs_registry \gset
\if :needs_registry
    \ir ../db/migrations/202_config_registry.sql
\endif

-- Abort on a partially installed registry instead of starting with stale config.
SELECT position FROM config_objects LIMIT 0;
SELECT alert_id, team_id, approved_version FROM rule_proposals LIMIT 0;
DO $check$
BEGIN
    IF NOT EXISTS (SELECT FROM config_state WHERE singleton)
       OR NOT EXISTS (SELECT FROM pg_trigger WHERE tgrelid = 'config_objects'::regclass
                      AND tgname = 'config_revision_changed' AND tgenabled IN ('O', 'A')) THEN
        RAISE EXCEPTION 'Incomplete config registry migration (202)';
    END IF;
END
$check$;
\ir ../db/migrations/137_rule_path_samples.sql
\ir ../db/migrations/215_log_tail_checkpoints.sql
COMMIT;
