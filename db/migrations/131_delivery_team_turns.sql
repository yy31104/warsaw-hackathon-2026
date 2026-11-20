-- Only claim transactions update these turns. Provider latency never holds a team lock.
create sequence if not exists delivery_team_claim_order;
create table if not exists delivery_team_turns (
    channels text[] not null,
    fallback boolean not null,
    priority integer not null,
    team_id text not null,
    last_turn bigint not null default nextval('delivery_team_claim_order'),
    primary key (channels, fallback, priority, team_id)
);
