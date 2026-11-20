-- 110_contract_kinds.sql — shared contract prep for the parallel Yi workstreams (feat/yi-contract).
-- Widens notification kinds; workstreams own later migrations: W1 115-124, W2 125-134, W3 135-139, W0 111-114.
alter table notifications drop constraint if exists notifications_kind_check;
alter table notifications add constraint notifications_kind_check check (kind in (
  'initial','update','digest','escalation','approval_request','throttle_summary',
  'merged','ladder_step','heartbeat','canary'));
