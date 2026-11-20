-- One index seek per active source lets the worker find the quiet source behind a burst.
create index if not exists events_pending_by_source on events (lane, source_id, id)
where status = 'pending';

-- Independent lanes and nontransactional turns: no shared row lock during rule evaluation.
create sequence if not exists event_normal_claim_turn;
create sequence if not exists event_critical_claim_turn;
