create unique index if not exists active_approval_code on action_runs(approval_code) where state='pending_approval';
alter table action_runs add column if not exists resolves_alert boolean not null default false;
