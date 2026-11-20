create table if not exists heartbeat_watches (
  rule_id text not null,
  alert_key text not null,
  rule_version integer not null,
  group_values jsonb not null,
  last_seen_at timestamptz not null,
  last_event_id bigint not null references events(id),
  due_at timestamptz,
  alert_id bigint references alerts(id),
  primary key (rule_id, alert_key)
);

create index if not exists heartbeat_watches_due on heartbeat_watches(due_at, rule_id, alert_key)
  where due_at is not null;
