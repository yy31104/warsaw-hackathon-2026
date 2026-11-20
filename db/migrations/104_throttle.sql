create table if not exists delivery_windows (
    rule_id text not null,
    recipient text not null,
    channel text not null,
    bucket bigint not null,
    hits bigint not null,
    expires_at timestamptz not null,
    primary key(rule_id,recipient,channel,bucket)
);
create index if not exists delivery_windows_expiry on delivery_windows(expires_at);
alter table notifications add column if not exists throttle_bucket text unique;
create unique index if not exists notifications_single_fallback on notifications(fallback_of) where fallback_of is not null;
