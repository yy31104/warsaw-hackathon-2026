-- NULL on events accepted before onboarding existed. The review service indexes
-- legacy needs_review rows on the first list or approval, using the configured team.
ALTER TABLE events ADD COLUMN IF NOT EXISTS review_fingerprint text;
CREATE INDEX IF NOT EXISTS events_awaiting_schema ON events (review_fingerprint, id)
    WHERE status = 'needs_review';
