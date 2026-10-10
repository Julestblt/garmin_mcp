create or replace function public.upsert_activity_track(
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
    nullif(p_track->'route', 'null'::jsonb),
    nullif(p_track->'bounds', 'null'::jsonb),
    nullif(p_track->'series', 'null'::jsonb),
    nullif(p_track->'heart_rate_histogram', 'null'::jsonb),
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
