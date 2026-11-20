alter table alerts add column spike_state jsonb not null default '{}'::jsonb
    check (jsonb_typeof(spike_state) = 'object');
