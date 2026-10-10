alter table public.provider_activities
  add column activity_name text,
  add column calories numeric,
  add column workout_rpe numeric,
  add column workout_feel numeric;

create table public.provider_activity_tracks (
  activity_id uuid primary key references public.provider_activities(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null,
  status text not null check (status in ('available', 'unavailable')),
  route jsonb check (route is null or jsonb_typeof(route) = 'array'),
  bounds jsonb check (bounds is null or jsonb_typeof(bounds) = 'object'),
  series jsonb check (series is null or jsonb_typeof(series) = 'object'),
  heart_rate_histogram jsonb
    check (heart_rate_histogram is null or jsonb_typeof(heart_rate_histogram) = 'object'),
  route_point_count integer not null default 0,
  series_point_count integer not null default 0,
  source_point_count integer not null default 0,
  source_updated_at timestamptz,
  imported_at timestamptz not null default now()
);
create index provider_activity_tracks_user_idx
  on public.provider_activity_tracks (user_id, activity_id);
alter table public.provider_activity_tracks enable row level security;
create policy activity_tracks_owner_read on public.provider_activity_tracks
  for select to authenticated using (user_id = (select auth.uid()));
grant select on public.provider_activity_tracks to authenticated;
grant all on public.provider_activity_tracks to service_role;

create or replace function public.upsert_garmin_activity(
  p_connection_id uuid, p_user_id uuid, p_job_id uuid, p_attempts integer,
  p_activity jsonb, p_segments jsonb
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
  perform 1 from public.sync_jobs
  where id = p_job_id and connection_id = p_connection_id and user_id = p_user_id
    and state = 'running' and attempts = p_attempts and lease_expires_at > now()
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
    activity_name, calories, workout_rpe, workout_feel,
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
    p_activity->>'activity_name', (p_activity->>'calories')::numeric,
    (p_activity->>'workout_rpe')::numeric, (p_activity->>'workout_feel')::numeric,
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
    activity_name = excluded.activity_name,
    calories = excluded.calories,
    workout_rpe = excluded.workout_rpe,
    workout_feel = excluded.workout_feel,
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

create function public.list_activities_missing_tracks(
  p_connection_id uuid, p_since timestamptz, p_limit integer
)
returns table (id uuid, provider_activity_id text, source_updated_at timestamptz)
language sql stable security definer set search_path = public
as $$
  select activity.id, activity.provider_activity_id, activity.source_updated_at
  from public.provider_activities activity
  left join public.provider_activity_tracks track on track.activity_id = activity.id
  where activity.connection_id = p_connection_id
    and activity.provider = 'garmin'
    and activity.started_at >= p_since
    and (
      track.activity_id is null
      or track.source_updated_at is distinct from activity.source_updated_at
      or (track.status = 'unavailable' and track.imported_at < now() - interval '1 day')
    )
  order by activity.started_at desc
  limit least(greatest(p_limit, 1), 20);
$$;
revoke all on function public.list_activities_missing_tracks(uuid, timestamptz, integer)
  from public, anon, authenticated;
grant execute on function public.list_activities_missing_tracks(uuid, timestamptz, integer)
  to service_role;

create function public.upsert_activity_track(
  p_connection_id uuid, p_user_id uuid, p_job_id uuid, p_attempts integer,
  p_activity_id uuid, p_track jsonb
)
returns boolean
language plpgsql security definer set search_path = public
as $$
declare activity_source_updated_at timestamptz;
begin
  perform 1 from public.provider_connections
  where id = p_connection_id and user_id = p_user_id
    and provider = 'garmin' and status = 'connected'
  for update;
  if not found then
    return false;
  end if;
  perform 1 from public.sync_jobs
  where id = p_job_id and connection_id = p_connection_id and user_id = p_user_id
    and state = 'running' and attempts = p_attempts and lease_expires_at > now()
  for update;
  if not found then
    return false;
  end if;
  select activity.source_updated_at into activity_source_updated_at
  from public.provider_activities activity
  where activity.id = p_activity_id and activity.user_id = p_user_id
    and activity.connection_id = p_connection_id and activity.provider = 'garmin';
  if not found then
    return false;
  end if;
  insert into public.provider_activity_tracks (
    activity_id, user_id, provider, status, route, bounds, series,
    heart_rate_histogram, route_point_count, series_point_count,
    source_point_count, source_updated_at
  ) values (
    p_activity_id, p_user_id, 'garmin', p_track->>'status',
    p_track->'route', p_track->'bounds', p_track->'series',
    p_track->'heart_rate_histogram',
    coalesce((p_track->>'route_point_count')::integer, 0),
    coalesce((p_track->>'series_point_count')::integer, 0),
    coalesce((p_track->>'source_point_count')::integer, 0),
    activity_source_updated_at
  )
  on conflict (activity_id) do update set
    status = excluded.status,
    route = excluded.route,
    bounds = excluded.bounds,
    series = excluded.series,
    heart_rate_histogram = excluded.heart_rate_histogram,
    route_point_count = excluded.route_point_count,
    series_point_count = excluded.series_point_count,
    source_point_count = excluded.source_point_count,
    source_updated_at = excluded.source_updated_at,
    imported_at = now();
  return true;
end;
$$;
revoke all on function public.upsert_activity_track(uuid, uuid, uuid, integer, uuid, jsonb)
  from public, anon, authenticated;
grant execute on function public.upsert_activity_track(uuid, uuid, uuid, integer, uuid, jsonb)
  to service_role;

create or replace function public.schedule_incremental_sync()
returns integer
language plpgsql security definer set search_path = public
as $$
declare scheduled integer;
declare scheduled_tracks integer;
begin
  insert into public.sync_jobs (
    connection_id, user_id, provider, mode, state, phase,
    cursor_date, oldest_date
  )
  select connection.id, connection.user_id, 'garmin', 'incremental', 'queued',
         historical.phase, current_date,
         greatest(historical.oldest_date,
                  current_date - case when historical.phase = 'activities' then 14 else 7 end)
  from public.provider_connections connection
  join public.sync_jobs historical on historical.connection_id = connection.id
    and historical.mode = 'historical' and historical.state = 'succeeded'
    and historical.phase in ('activities', 'recovery', 'fitness')
  where connection.provider = 'garmin' and connection.status = 'connected'
    and not exists (
      select 1 from public.sync_jobs recent
      where recent.connection_id = connection.id and recent.mode = 'incremental'
        and recent.phase = historical.phase
        and (recent.state in ('queued', 'running')
             or recent.created_at > now() - interval '6 hours')
    )
  on conflict do nothing;
  get diagnostics scheduled = row_count;
  insert into public.sync_jobs (
    connection_id, user_id, provider, mode, state, phase, cursor_date, oldest_date
  )
  select connection.id, connection.user_id, 'garmin', 'incremental', 'queued',
         'tracks', current_date, current_date
  from public.provider_connections connection
  join public.sync_jobs historical on historical.connection_id = connection.id
    and historical.mode = 'historical' and historical.state = 'succeeded'
    and historical.phase = 'activities'
  where connection.provider = 'garmin' and connection.status = 'connected'
    and not exists (
      select 1 from public.sync_jobs recent
      where recent.connection_id = connection.id and recent.mode = 'incremental'
        and recent.phase = 'tracks'
        and (recent.state in ('queued', 'running')
             or recent.created_at > now() - interval '1 hour')
    )
  on conflict do nothing;
  get diagnostics scheduled_tracks = row_count;
  return scheduled + scheduled_tracks;
end;
$$;

insert into public.sync_jobs (
  connection_id, user_id, provider, mode, state, phase, cursor_date, oldest_date
)
select connection.id, connection.user_id, 'garmin', 'incremental', 'queued',
       'activities', current_date, greatest(historical.oldest_date, current_date - 90)
from public.provider_connections connection
join public.sync_jobs historical on historical.connection_id = connection.id
  and historical.mode = 'historical' and historical.state = 'succeeded'
  and historical.phase = 'activities'
where connection.provider = 'garmin' and connection.status = 'connected'
on conflict do nothing;
