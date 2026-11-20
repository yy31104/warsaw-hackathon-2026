-- 001_init.sql — shared base schema (see docs/CONTRACT.md §7).
-- Ranges for later files: 0xx DATA, 1xx BACKEND. Add files; never edit merged ones.
-- Applied automatically on a fresh volume (docker-entrypoint-initdb.d). `make reset-db` to re-apply.

create extension if not exists pgcrypto;

-- Versioned config for every kind (source, schema, rule, team, user, runbook, singleton).
create table if not exists config_objects (
  kind          text        not null,
  id            text        not null,
  version       int         not null,
  spec          jsonb       not null,
  content_hash  text        not null,
  origin        text        not null check (origin in ('file','ui','api')),
  active        boolean     not null default true,
  created_at    timestamptz not null default now(),
  created_by    text,
  primary key (kind, id, version)
);
create unique index if not exists config_objects_active on config_objects (kind, id) where active;

-- INBOX
create table if not exists events (
  id               bigserial   primary key,
  source_id        text        not null,
  transport        text        not null check (transport in ('http','rabbitmq','logfile')),
  source_event_id  text,
  payload_hash     text        not null,
  schema_id        text,
  schema_version   int,
  schema_name      text,
  severity         text        not null default 'unknown',
  service          text,
  environment      text,
  occurred_at      timestamptz,
  received_at      timestamptz not null default now(),
  raw              jsonb       not null,
  fields           jsonb       not null default '{}'::jsonb,
  lane             text        not null default 'normal' check (lane in ('critical','normal')),
  status           text        not null default 'pending'
                   check (status in ('pending','processed','needs_review','rejected')),
  processed_at     timestamptz,
  trace            jsonb       not null default '{}'::jsonb
);
create unique index if not exists events_identity on events (source_id, source_event_id) where source_event_id is not null;
create index if not exists events_pending on events (lane, id) where status = 'pending';
create index if not exists events_received on events (received_at);

create table if not exists ingest_rejections (
  id               bigserial   primary key,
  source_id        text        not null,
  source_event_id  text,
  kind             text        not null check (kind in ('DUPLICATE','CONFLICT','REJECTED')),
  reason           text,
  existing_event_id bigint,
  received_at      timestamptz not null default now()
);

create table if not exists schema_issues (
  fingerprint      text        primary key,          -- sorted top-level keys
  source_id        text        not null,
  sample_event_id  bigint,
  sample_fields    jsonb       not null,
  event_count      bigint      not null default 1,
  status           text        not null default 'open' check (status in ('open','resolved','ignored')),
  first_seen_at    timestamptz not null default now(),
  last_seen_at     timestamptz not null default now(),
  resolved_schema_id text
);

-- ALERTS (one open row per rule + alert_key)
create table if not exists alerts (
  id                      bigserial   primary key,
  rule_id                 text        not null,
  rule_version            int         not null,
  alert_key               text        not null,
  status                  text        not null default 'open' check (status in ('open','acknowledged','resolved')),
  severity                text        not null default 'unknown',
  priority                text,
  score                   numeric,
  score_breakdown         jsonb,
  lane                    text        not null default 'normal',
  team_id                 text,
  title                   text,
  body                    text,
  color                   text,
  fields                  jsonb       not null default '{}'::jsonb,
  group_values            jsonb       not null default '{}'::jsonb,
  runbook_id              text,
  runbook_version         int,
  occurrence_count        bigint      not null default 0,
  count_since_notify      bigint      not null default 0,
  first_seen_at           timestamptz,
  last_seen_at            timestamptz,
  representative_event_id bigint,
  last_notified_at        timestamptz,
  acked_by                text,
  acked_at                timestamptz,
  resolved_by             text,
  resolved_at             timestamptz,
  resolve_reason          text,
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now()
);
create unique index if not exists alerts_open_key on alerts (rule_id, alert_key) where status <> 'resolved';
create index if not exists alerts_team_status on alerts (team_id, status, last_seen_at desc);

create table if not exists alert_occurrences (
  alert_id    bigint      not null references alerts(id),
  event_id    bigint      not null references events(id),
  seen_at     timestamptz not null default now(),
  primary key (alert_id, event_id)
);

create table if not exists decisions (
  id            bigserial   primary key,
  event_id      bigint      not null references events(id),
  rule_id       text,
  rule_version  int,
  outcome       text        not null,
  reason        jsonb,
  alert_id      bigint,
  created_at    timestamptz not null default now()
);
create index if not exists decisions_event on decisions (event_id);
create index if not exists decisions_alert on decisions (alert_id);

-- OUTBOX
create table if not exists notifications (
  id               uuid        primary key default gen_random_uuid(),
  alert_id         bigint      not null references alerts(id),
  kind             text        not null check (kind in ('initial','update','digest','escalation','approval_request','throttle_summary')),
  recipient        text        not null,     -- user id, or team:<id> for ui
  channel          text        not null check (channel in ('email','sms','phone','ui')),
  contact          text,
  rendered         jsonb       not null,
  priority         int         not null default 3,
  state            text        not null default 'pending'
                   check (state in ('pending','sending','delivered','retrying','unknown','failed','fallback','cancelled')),
  attempts         int         not null default 0,
  next_attempt_at  timestamptz not null default now(),
  locked_until     timestamptz,
  locked_by        text,
  dedup_key        text,
  fallback_of      uuid,
  created_at       timestamptz not null default now(),
  delivered_at     timestamptz
);
create unique index if not exists notifications_dedup on notifications (dedup_key) where dedup_key is not null;
create index if not exists notifications_due on notifications (channel, priority, next_attempt_at)
  where state in ('pending','retrying','unknown');
create index if not exists notifications_alert on notifications (alert_id);

create table if not exists delivery_attempts (
  id               bigserial   primary key,
  notification_id  uuid        not null references notifications(id),
  attempt          int         not null,
  started_at       timestamptz not null default now(),
  finished_at      timestamptz,
  outcome          text        check (outcome in ('delivered','retryable','permanent','unknown')),
  provider_ref     text,
  error            text,
  latency_ms       int
);
create index if not exists delivery_attempts_notification on delivery_attempts (notification_id);

-- RUNBOOK ACTIONS, APPROVALS, AUDIT
create table if not exists action_runs (
  id                  bigserial   primary key,
  alert_id            bigint      not null references alerts(id),
  runbook_id          text,
  step_index          int,
  action              text        not null,
  params              jsonb       not null default '{}'::jsonb,
  approval            text        not null check (approval in ('none','required')),
  state               text        not null default 'requested'
                      check (state in ('requested','pending_approval','approved','running','succeeded','failed','rejected')),
  approval_code       text,
  approval_expires_at timestamptz,
  requested_by        text        not null,
  approved_by         text,
  result              jsonb,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);
create index if not exists action_runs_alert on action_runs (alert_id);

create table if not exists audit_log (
  id           bigserial   primary key,
  at           timestamptz not null default now(),
  actor        text        not null,          -- user id or 'system' or 'llm-suggestion:<user>'
  action       text        not null,          -- alert.ack, alert.resolve, rule.publish, action.execute, action.approve, schema.create ...
  target_kind  text,
  target_id    text,
  detail       jsonb
);
create index if not exists audit_target on audit_log (target_kind, target_id);

create table if not exists rule_proposals (
  id            bigserial   primary key,
  rule_id       text        not null,
  base_version  int         not null,
  draft         jsonb       not null,
  rationale     text,
  source        text        not null check (source in ('human','llm')),
  proposed_by   text        not null,
  preview       jsonb,
  status        text        not null default 'open' check (status in ('open','approved','rejected')),
  decided_by    text,
  created_at    timestamptz not null default now(),
  decided_at    timestamptz
);
