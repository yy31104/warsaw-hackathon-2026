-- Read the newest eligible samples per registered schema for advisory rule-field checks.
CREATE INDEX IF NOT EXISTS events_rule_path_samples
    ON events (schema_id, received_at DESC, id DESC)
    WHERE status IN ('pending', 'processed');
