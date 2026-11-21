alter table action_runs drop constraint action_runs_state_check;
alter table action_runs add constraint action_runs_state_check check
    (state in ('requested','pending_approval','approved','running','succeeded','failed','unknown','rejected'));
