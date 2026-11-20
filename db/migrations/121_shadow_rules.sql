-- Each dry-run revision has its own incidents, occurrence counts and projected cooldowns.
create table shadow_alerts (
  id                bigserial primary key,
  rule_id           text not null,
  rule_version      integer not null,
  alert_key         text not null,
  status            text not null default 'open' check (status in ('open', 'resolved')),
  severity          text not null,
  latest_episode    text,
  occurrence_count  bigint not null,
  count_since_notify bigint not null,
  last_notified_at  timestamptz, -- last eligible dry-run transition
  spike_state       jsonb
);
create unique index shadow_alerts_open on shadow_alerts (rule_id, rule_version, alert_key)
  where status = 'open';
create index shadow_alerts_resolved_episode on shadow_alerts (rule_id, rule_version, alert_key)
  where status = 'resolved' and latest_episode is not null;

-- A retry of the same stored event and draft must not advance its state twice.
create unique index decisions_shadow_once on decisions (event_id, rule_id, rule_version)
  where outcome = 'SHADOW_MATCH' or (outcome = 'RULE_FAULT' and reason->>'shadow' = 'true');
