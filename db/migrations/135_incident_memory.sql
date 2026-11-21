-- A card is a snapshot of a manually resolved alert, confirmed by its resolver.
create table if not exists incident_memory (
    alert_id bigint primary key references alerts(id) on delete cascade,
    team_id text not null,
    service text,
    error_code text,
    schema_id text,
    schema_name text,
    schema_version integer,
    runbook_id text,
    runbook_version integer,
    actions_attempted jsonb not null check (jsonb_typeof(actions_attempted) = 'array'),
    verified_resolution text not null check (btrim(verified_resolution) <> ''),
    notes text not null check (btrim(notes) <> ''),
    confirmed_by text not null check (confirmed_by <> 'system' and confirmed_by not like 'llm-suggestion:%'),
    resolved_at timestamptz not null,
    confirmed_at timestamptz not null,
    resolution_audit_id bigint not null references audit_log(id) on delete cascade
);

create index if not exists incident_memory_team on incident_memory(team_id, resolved_at desc);
