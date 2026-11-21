alter table alerts
  add column if not exists escalation_stage integer not null default 0,
  add column if not exists ladder_stage integer not null default 0;
