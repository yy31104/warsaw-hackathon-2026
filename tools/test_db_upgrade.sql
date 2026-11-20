-- Run by test_isolated.ps1 in a disposable DB populated from the main baseline.
\set ON_ERROR_STOP on
DO $test$
BEGIN
    IF current_database() <> 'upgrade_test' OR to_regclass('config_state') IS NOT NULL THEN
        RAISE EXCEPTION 'Expected a disposable database at the pre-registry baseline';
    END IF;
END
$test$;
INSERT INTO config_objects(kind,id,version,spec,content_hash,origin)
    VALUES ('rule','upgrade-probe',3,'{"enabled":true}','probe','ui');
INSERT INTO audit_log(actor,action) VALUES ('upgrade-probe','rule.publish');
\ir upgrade_db.sql
UPDATE config_objects SET position=7 WHERE id='upgrade-probe';
INSERT INTO log_tail_checkpoints(source_id,path,byte_offset) VALUES ('probe','demo.log',42);
\ir upgrade_db.sql
DO $test$
BEGIN
    IF NOT EXISTS (SELECT FROM config_objects WHERE id='upgrade-probe' AND position=7 AND version=3
                   AND spec='{"enabled":true}'::jsonb AND active)
       OR NOT EXISTS (SELECT FROM config_state WHERE singleton AND revision=1)
       OR NOT EXISTS (SELECT FROM audit_log WHERE actor='upgrade-probe' AND action='rule.publish')
       OR NOT EXISTS (SELECT FROM log_tail_checkpoints WHERE source_id='probe' AND byte_offset=42) THEN
        RAISE EXCEPTION 'Upgrade lost stored data or failed to track a config change';
    END IF;
END
$test$;
