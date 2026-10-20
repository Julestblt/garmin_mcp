create table public.provider_gear (
  connection_id uuid not null references public.provider_connections(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null default 'garmin' check (provider = 'garmin'),
  gear_uuid text not null check (gear_uuid ~ '^[0-9a-f]{32}$'),
  gear_type text not null check (gear_type = 'shoes'),
  brand text,
  model text,
  display_name text,
  status text not null check (status in ('active', 'retired')),
  first_use_date date,
  retired_on date,
  maximum_meters double precision check (maximum_meters is null or maximum_meters > 0),
  total_distance_meters double precision not null default 0 check (total_distance_meters >= 0),
  total_activities integer not null default 0 check (total_activities >= 0),
  updated_at timestamptz not null default now(),
  primary key (user_id, provider, gear_uuid)
);
create index provider_gear_connection_idx on public.provider_gear (connection_id);
alter table public.provider_gear enable row level security;
create policy gear_owner_read on public.provider_gear
  for select to authenticated using (user_id = (select auth.uid()));
grant select on public.provider_gear to authenticated;
grant all on public.provider_gear to service_role;

create table public.provider_activity_gear (
  connection_id uuid not null references public.provider_connections(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null default 'garmin' check (provider = 'garmin'),
  provider_activity_id text not null check (provider_activity_id ~ '^[0-9]{1,20}$'),
  gear_uuid text not null check (gear_uuid ~ '^[0-9a-f]{32}$'),
  updated_at timestamptz not null default now(),
  primary key (user_id, provider, provider_activity_id)
);
create index provider_activity_gear_connection_idx on public.provider_activity_gear (connection_id);
create index provider_activity_gear_gear_idx
  on public.provider_activity_gear (user_id, provider, gear_uuid);
alter table public.provider_activity_gear enable row level security;
create policy activity_gear_owner_read on public.provider_activity_gear
  for select to authenticated using (user_id = (select auth.uid()));
grant select on public.provider_activity_gear to authenticated;
grant all on public.provider_activity_gear to service_role;
