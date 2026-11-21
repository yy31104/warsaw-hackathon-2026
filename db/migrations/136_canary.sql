-- Stamp trusted transport provenance at the durable inbox boundary. Producers cannot
-- opt themselves in by including a synthetic flag in their raw payload.
create function mark_canary_event() returns trigger language plpgsql as $$
begin
  new.trace := coalesce(new.trace, '{}'::jsonb) || '{"synthetic":true}'::jsonb;
  return new;
end
$$;

create trigger stamp_canary_event before insert or update of trace, source_id on events
for each row when (new.source_id = 'canary' and (new.trace->'synthetic') is distinct from 'true'::jsonb)
execute function mark_canary_event();

update events set trace=trace where source_id='canary'
and (trace->'synthetic') is distinct from 'true'::jsonb;

-- Only the external watchdog schedules synthetic pulses.
create function canary_disable_reminders() returns trigger language plpgsql as $$
begin
  new.next_notify_at := null;
  return new;
end
$$;

create trigger no_canary_reminders before insert or update of next_notify_at on alerts
for each row when (new.rule_id = 'delivery-canary' and new.next_notify_at is not null)
execute function canary_disable_reminders();

-- A misconfigured destination remains visible as cancelled and cannot be claimed.
create function canary_notification() returns trigger language plpgsql as $$
begin
  new.kind := 'canary';
  if new.channel <> 'sms' or new.recipient <> 'canary'
     or new.contact is distinct from '+15550100990' then
    new.state := 'cancelled';
    new.delivered_at := null;
    new.attempts := 0;
  end if;
  return new;
end
$$;

create trigger tag_canary_notification before insert on notifications
for each row when (new.rendered #>> '{meta,rule_id}' = 'delivery-canary')
execute function canary_notification();
