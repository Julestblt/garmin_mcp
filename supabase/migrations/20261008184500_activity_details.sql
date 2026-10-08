alter table public.provider_activities
  add column local_started_at timestamp,
  add column time_zone_id text,
  add column elevation_gain_meters numeric,
  add column elevation_loss_meters numeric,
  add column average_heart_rate numeric,
  add column max_heart_rate numeric,
  add column average_cadence numeric,
  add column average_power_watts numeric,
  add column max_power_watts numeric,
  add column aerobic_training_effect numeric,
  add column anaerobic_training_effect numeric,
  add column route_name text,
  add column has_route boolean,
  add column detail_imported_at timestamptz,
  add column imported_at timestamptz not null default now();

create table public.provider_activity_segments (
  id uuid primary key default gen_random_uuid(),
  activity_id uuid not null references public.provider_activities(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null,
  segment_type text not null check (segment_type in ('lap', 'split')),
  segment_index integer not null,
  started_at timestamptz,
  distance_meters numeric,
  duration_seconds numeric,
  average_heart_rate numeric,
  average_cadence numeric,
  average_power_watts numeric,
  elevation_gain_meters numeric,
  elevation_loss_meters numeric,
  imported_at timestamptz not null default now(),
  unique (activity_id, segment_type, segment_index)
);
create index provider_activity_segments_user_idx
  on public.provider_activity_segments (user_id, activity_id);
alter table public.provider_activity_segments enable row level security;
create policy activity_segments_owner_read on public.provider_activity_segments
  for select to authenticated using (user_id = auth.uid());
grant select on public.provider_activity_segments to authenticated;
grant all on public.provider_activity_segments to service_role;

create function public.upsert_garmin_activity(
  p_connection_id uuid, p_user_id uuid, p_activity jsonb, p_segments jsonb
)
returns boolean
language plpgsql security definer set search_path = public
as $$
declare activity_row_id uuid;
declare segment jsonb;
begin
  perform 1 from public.provider_connections
  where id = p_connection_id and user_id = p_user_id
    and provider = 'garmin' and status = 'connected'
  for update;
  if not found then
    return false;
  end if;
  insert into public.provider_activities (
    user_id, connection_id, provider, provider_activity_id, activity_type,
    started_at, local_started_at, time_zone_id, distance_meters, duration_seconds,
    elevation_gain_meters, elevation_loss_meters, average_heart_rate,
    max_heart_rate, average_cadence, average_power_watts, max_power_watts,
    aerobic_training_effect, anaerobic_training_effect, route_name, has_route,
    source_updated_at, detail_imported_at
  ) values (
    p_user_id, p_connection_id, 'garmin', p_activity->>'provider_activity_id',
    p_activity->>'activity_type', (p_activity->>'started_at')::timestamptz,
    (p_activity->>'local_started_at')::timestamp, p_activity->>'time_zone_id',
    (p_activity->>'distance_meters')::numeric, (p_activity->>'duration_seconds')::numeric,
    (p_activity->>'elevation_gain_meters')::numeric,
    (p_activity->>'elevation_loss_meters')::numeric,
    (p_activity->>'average_heart_rate')::numeric,
    (p_activity->>'max_heart_rate')::numeric,
    (p_activity->>'average_cadence')::numeric,
    (p_activity->>'average_power_watts')::numeric,
    (p_activity->>'max_power_watts')::numeric,
    (p_activity->>'aerobic_training_effect')::numeric,
    (p_activity->>'anaerobic_training_effect')::numeric,
    p_activity->>'route_name', (p_activity->>'has_route')::boolean,
    (p_activity->>'source_updated_at')::timestamptz, now()
  )
  on conflict (user_id, provider, provider_activity_id) do update set
    connection_id = excluded.connection_id,
    activity_type = excluded.activity_type,
    started_at = excluded.started_at,
    local_started_at = excluded.local_started_at,
    time_zone_id = excluded.time_zone_id,
    distance_meters = excluded.distance_meters,
    duration_seconds = excluded.duration_seconds,
    elevation_gain_meters = excluded.elevation_gain_meters,
    elevation_loss_meters = excluded.elevation_loss_meters,
    average_heart_rate = excluded.average_heart_rate,
    max_heart_rate = excluded.max_heart_rate,
    average_cadence = excluded.average_cadence,
    average_power_watts = excluded.average_power_watts,
    max_power_watts = excluded.max_power_watts,
    aerobic_training_effect = excluded.aerobic_training_effect,
    anaerobic_training_effect = excluded.anaerobic_training_effect,
    route_name = excluded.route_name,
    has_route = excluded.has_route,
    source_updated_at = excluded.source_updated_at,
    detail_imported_at = now(), updated_at = now()
  returning id into activity_row_id;
  delete from public.provider_activity_segments where activity_id = activity_row_id;
  for segment in select value from jsonb_array_elements(p_segments)
  loop
    insert into public.provider_activity_segments (
      activity_id, user_id, provider, segment_type, segment_index,
      started_at, distance_meters, duration_seconds, average_heart_rate,
      average_cadence, average_power_watts, elevation_gain_meters,
      elevation_loss_meters
    ) values (
      activity_row_id, p_user_id, 'garmin', segment->>'segment_type',
      (segment->>'segment_index')::integer,
      (segment->>'started_at')::timestamptz,
      (segment->>'distance_meters')::numeric,
      (segment->>'duration_seconds')::numeric,
      (segment->>'average_heart_rate')::numeric,
      (segment->>'average_cadence')::numeric,
      (segment->>'average_power_watts')::numeric,
      (segment->>'elevation_gain_meters')::numeric,
      (segment->>'elevation_loss_meters')::numeric
    );
  end loop;
  return true;
end;
$$;
revoke all on function public.upsert_garmin_activity(uuid, uuid, jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function public.upsert_garmin_activity(uuid, uuid, jsonb, jsonb)
  to service_role;
