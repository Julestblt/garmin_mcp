create table public.provider_workouts (
  connection_id uuid not null references public.provider_connections(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null default 'garmin' check (provider = 'garmin'),
  external_key text not null check (external_key ~ '^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$'),
  scheduled_on date not null,
  status text not null check (status in ('sent', 'failed', 'needs_confirmation')),
  content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
  provider_workout_id bigint,
  error_code text,
  simplifications text[] not null default '{}',
  sent_at timestamptz,
  updated_at timestamptz not null default now(),
  primary key (user_id, provider, external_key),
  check (status <> 'sent' or (provider_workout_id is not null and sent_at is not null)),
  check (status <> 'failed' or error_code is not null)
);
create index provider_workouts_connection_idx on public.provider_workouts (connection_id);
create index provider_workouts_user_scheduled_idx
  on public.provider_workouts (user_id, scheduled_on);
alter table public.provider_workouts enable row level security;
create policy workouts_owner_read on public.provider_workouts
  for select to authenticated using (user_id = (select auth.uid()));
grant select on public.provider_workouts to authenticated;
grant all on public.provider_workouts to service_role;
