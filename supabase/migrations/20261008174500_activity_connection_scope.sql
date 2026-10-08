alter table public.provider_activities
  add column connection_id uuid references public.provider_connections(id) on delete cascade;

update public.provider_activities activity
set connection_id = connection.id
from public.provider_connections connection
where activity.user_id = connection.user_id and activity.provider = connection.provider;

delete from public.provider_activities where connection_id is null;

alter table public.provider_activities alter column connection_id set not null;
create index provider_activities_connection_idx on public.provider_activities (connection_id, started_at desc);
