CREATE TABLE silences (
    id bigserial PRIMARY KEY,
    team_id text NOT NULL,
    matcher jsonb NOT NULL CHECK (jsonb_typeof(matcher) = 'object'),
    include_critical boolean NOT NULL DEFAULT false,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > created_at),
    revoked_at timestamptz
);

CREATE INDEX silences_active_expiry ON silences (expires_at, team_id) WHERE revoked_at IS NULL;
