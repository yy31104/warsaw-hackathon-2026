-- File detection order is runtime configuration, independent of object content versions.
alter table config_objects add column position integer;

create table config_state (
    singleton boolean primary key default true check (singleton),
    revision bigint not null default 0,
    files_imported boolean not null default false
);
insert into config_state default values;

-- Workers can check a single row against their compiled snapshot even after a LISTEN gap.
create function advance_config_revision() returns trigger language plpgsql as $$
begin
    update config_state set revision = revision + 1 where singleton;
    return null;
end;
$$;

create trigger config_revision_changed
after insert or update or delete or truncate on config_objects
for each statement execute function advance_config_revision();

alter table rule_proposals
    add column alert_id bigint,
    add column team_id text,
    add column approved_version integer;

create index rule_proposals_rule_status on rule_proposals (rule_id, status, id desc);
