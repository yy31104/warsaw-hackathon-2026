-- Snapshot the trusted schema declaration for later manual escalations.
-- NULL identifies an older incident whose policy must be looked up by schema.
alter table alerts add column if not exists sensitive_paths text[];
