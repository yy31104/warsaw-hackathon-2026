-- Every incident retains its logical notification UUID and original alert link.
-- Only the primary row is eligible for provider delivery.
alter table notifications add column if not exists merged_into uuid references notifications(id);
create index if not exists notifications_merged_into on notifications(merged_into) where merged_into is not null;

create or replace function sync_merged_notifications() returns trigger language plpgsql as $$
declare
    member record;
begin
    for member in
        update notifications n set state=new.state, attempts=new.attempts, delivered_at=new.delivered_at
        from alerts a where n.merged_into=new.id and n.alert_id=a.id
        returning n.id,n.alert_id,a.team_id
    loop
        perform pg_notify('notifications',json_build_object(
            'type','notification.updated','data',json_build_object(
                'id',member.id,'alert_id',member.alert_id,'team_id',member.team_id,
                'ts',clock_timestamp(),'state',new.state,'attempts',new.attempts))::text);
    end loop;
    return new;
end
$$;

create or replace trigger notifications_sync_merged
after update of state,attempts,delivered_at on notifications
for each row when (new.kind='merged' and new.merged_into is null)
execute function sync_merged_notifications();
