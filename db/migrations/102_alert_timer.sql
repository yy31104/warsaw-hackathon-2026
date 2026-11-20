alter table alerts add column if not exists next_notify_at timestamptz;
update alerts set next_notify_at=now() where next_notify_at is null and status='open' and count_since_notify>0;
create index if not exists alerts_due_timer on alerts(next_notify_at) where status='open' and next_notify_at is not null;
