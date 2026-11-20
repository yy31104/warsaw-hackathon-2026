create table approval_failures (sender text not null, failed_at timestamptz not null);
-- Four-digit codes already in flight can no longer be redeemed after this upgrade.
update action_runs set state='rejected',approval_code=null,updated_at=now()
where state='pending_approval' and length(approval_code)<>6;
