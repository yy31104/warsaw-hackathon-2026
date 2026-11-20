-- Keep the causal IDs for provider retries even after the representative event changes.
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS trace_event_id bigint;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS trace_run_id bigint;

CREATE OR REPLACE FUNCTION trace_incident_origin(incident bigint) RETURNS bigint
LANGUAGE sql STABLE AS $$
    SELECT event_id FROM decisions WHERE alert_id = incident ORDER BY id LIMIT 1
$$;

CREATE OR REPLACE FUNCTION trace_notification_context(n notifications)
RETURNS TABLE(event_id bigint, run_id bigint) LANGUAGE sql STABLE AS $$
    -- Follow pre-migration fallbacks until a cached trace or the original route.
    -- UNION also terminates a malformed cycle in legacy data.
    WITH RECURSIVE lineage(id, fallback_of, trace_event_id, trace_run_id, dedup_key, kind, recipient, bucket) AS (
        SELECT n.id, n.fallback_of, n.trace_event_id, n.trace_run_id, n.dedup_key,
               n.kind, n.recipient, n.throttle_bucket
        UNION
        SELECT p.id, p.fallback_of, p.trace_event_id, p.trace_run_id, p.dedup_key,
               p.kind, p.recipient, p.throttle_bucket
        FROM lineage l JOIN notifications p ON p.id = l.fallback_of AND p.alert_id = n.alert_id
        WHERE l.trace_event_id IS NULL
    ), origin AS (
        SELECT * FROM lineage WHERE trace_event_id IS NOT NULL OR fallback_of IS NULL LIMIT 1
    )
    SELECT coalesce(o.trace_event_id, route.event_id, trace_incident_origin(n.alert_id)),
           coalesce(o.trace_run_id, approval.id)
    FROM (VALUES (1)) AS anchor(dummy)
    LEFT JOIN origin o ON TRUE
    LEFT JOIN LATERAL (
        SELECT d.event_id FROM decisions d
        WHERE o.trace_event_id IS NULL AND d.alert_id = n.alert_id AND
              (d.reason->'notifications' ? o.dedup_key OR
               d.reason->'throttle_buckets' ? o.bucket)
        ORDER BY d.id LIMIT 1
    ) AS route ON TRUE
    LEFT JOIN action_runs approval ON o.kind = 'approval_request' AND approval.alert_id = n.alert_id
        AND o.dedup_key = concat_ws(':', n.alert_id, 'approval_request', o.recipient, 'sms', approval.id)
$$;

-- Workers write the outbox before the corresponding decisions in a batch. Defer
-- the trace to the end of the transaction so it sees the completed causal links
-- and a rolled-back processing/savepoint attempt produces no lifecycle lines.
CREATE OR REPLACE FUNCTION log_alert_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    stage text;
    status text;
    event_key bigint;
    alert_key bigint;
    notification_key uuid;
    run_key bigint;
    attempt_number integer;
    notice notifications%ROWTYPE;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'events' THEN
            stage := CASE TG_OP WHEN 'INSERT' THEN 'ingest' ELSE 'worker' END;
            event_key := NEW.id;
            status := NEW.status;
        WHEN 'decisions' THEN
            stage := 'rules';
            event_key := NEW.event_id;
            alert_key := NEW.alert_id;
            status := NEW.outcome;
        WHEN 'notifications' THEN
            stage := 'outbox';
            SELECT c.event_id, c.run_id INTO event_key, run_key FROM trace_notification_context(NEW) c;
            IF event_key IS NOT NULL OR run_key IS NOT NULL THEN
                UPDATE notifications SET trace_event_id = event_key, trace_run_id = run_key WHERE id = NEW.id;
            END IF;
            alert_key := NEW.alert_id;
            notification_key := NEW.id;
            status := NEW.state;
        WHEN 'delivery_attempts' THEN
            stage := 'delivery';
            SELECT * INTO notice FROM notifications WHERE id = NEW.notification_id;
            event_key := notice.trace_event_id;
            run_key := notice.trace_run_id;
            IF event_key IS NULL AND notice.id IS NOT NULL THEN
                -- Also handle deliveries queued before this migration.
                SELECT c.event_id, c.run_id INTO event_key, run_key FROM trace_notification_context(notice) c;
            END IF;
            alert_key := notice.alert_id;
            notification_key := NEW.notification_id;
            status := coalesce(NEW.outcome, 'sending');
            attempt_number := NEW.attempt;
        WHEN 'action_runs' THEN
            stage := 'runbook';
            alert_key := NEW.alert_id;
            event_key := trace_incident_origin(alert_key);
            run_key := NEW.id;
            status := NEW.state;
    END CASE;

    RAISE LOG 'alert_funnel %', json_build_object(
        'stage', stage, 'state', status, 'event_id', event_key, 'alert_id', alert_key,
        'notification_id', notification_key, 'run_id', run_key, 'attempt', attempt_number
    );
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS trace_ingest ON events;
CREATE CONSTRAINT TRIGGER trace_ingest AFTER INSERT ON events
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION log_alert_lifecycle();
DROP TRIGGER IF EXISTS trace_processed ON events;
CREATE CONSTRAINT TRIGGER trace_processed AFTER UPDATE OF status ON events
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION log_alert_lifecycle();
DROP TRIGGER IF EXISTS trace_rule ON decisions;
CREATE CONSTRAINT TRIGGER trace_rule AFTER INSERT ON decisions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.alert_id IS NOT NULL)
    EXECUTE FUNCTION log_alert_lifecycle();
DROP TRIGGER IF EXISTS trace_outbox ON notifications;
CREATE CONSTRAINT TRIGGER trace_outbox AFTER INSERT ON notifications
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION log_alert_lifecycle();
DROP TRIGGER IF EXISTS trace_claim ON delivery_attempts;
CREATE CONSTRAINT TRIGGER trace_claim AFTER INSERT ON delivery_attempts
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION log_alert_lifecycle();
DROP TRIGGER IF EXISTS trace_result ON delivery_attempts;
CREATE CONSTRAINT TRIGGER trace_result AFTER UPDATE OF outcome ON delivery_attempts
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (OLD.outcome IS DISTINCT FROM NEW.outcome)
    EXECUTE FUNCTION log_alert_lifecycle();
DROP TRIGGER IF EXISTS trace_run ON action_runs;
CREATE CONSTRAINT TRIGGER trace_run AFTER INSERT ON action_runs
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION log_alert_lifecycle();
DROP TRIGGER IF EXISTS trace_run_state ON action_runs;
CREATE CONSTRAINT TRIGGER trace_run_state AFTER UPDATE OF state ON action_runs
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state)
    EXECUTE FUNCTION log_alert_lifecycle();
