alter table alerts
  add column if not exists resolve_pending_at timestamptz,
  add column if not exists resolve_due_at timestamptz,
  add column if not exists resolve_episode text,
  add column if not exists resolve_event_id bigint references events(id);

create index if not exists alerts_recovery_due on alerts(resolve_due_at, id)
  where status <> 'resolved' and resolve_due_at is not null;
