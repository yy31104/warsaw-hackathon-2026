create index alerts_resolved_key on alerts (rule_id, alert_key) include (id)
    where status = 'resolved';

-- Several firing episodes can share an alert before it closes. Decisions retain every
-- match, including ones no longer present in the last-50 alert_occurrences sample.
create view resolved_alert_episodes as
    select rule_id, alert_key, latest_episode as episode, id as alert_id
    from alerts
    where status = 'resolved' and latest_episode is not null
    union
    select a.rule_id, a.alert_key, coalesce(d.reason->>'episode', e.fields->>'$episode'), a.id
    from alerts a
    join decisions d on d.alert_id = a.id
    join events e on e.id = d.event_id
    where a.status = 'resolved'
      and d.reason->>'transition' in ('created', 'occurrence', 'severity_increased', 'cooldown_elapsed')
      and coalesce(d.reason->>'episode', e.fields->>'$episode') is not null;
