alter table events drop constraint events_status_check;
alter table events add constraint events_status_check
    check (status in ('pending','processed','needs_review','rejected','quarantined'));
