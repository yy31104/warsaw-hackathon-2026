create table if not exists delivery_breakers (
    channel text primary key,
    failures int not null default 0,
    opened_at timestamptz,
    probe_after timestamptz
);
create index if not exists notifications_expired_lease on notifications(locked_until) where state='sending';
create unique index if not exists attempts_once on delivery_attempts(notification_id,attempt);
