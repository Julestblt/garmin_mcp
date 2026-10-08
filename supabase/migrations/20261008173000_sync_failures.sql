alter table public.sync_jobs add column failure_count integer not null default 0;
