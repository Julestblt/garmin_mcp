create view public.provider_training_weeks with (security_invoker = true) as
select
  user_id,
  provider,
  date_trunc('week', started_at)::date as week_start,
  count(*)::integer as activity_count,
  count(*) filter (where activity_type in (
    'running', 'trail_running', 'treadmill_running', 'track_running'
  ))::integer as run_count,
  coalesce(sum(distance_meters), 0) as distance_meters,
  coalesce(sum(duration_seconds), 0) as duration_seconds,
  max(source_updated_at) as latest_source_updated_at,
  max(imported_at) as latest_imported_at
from public.provider_activities
where started_at is not null
group by user_id, provider, date_trunc('week', started_at)::date;

grant select on public.provider_training_weeks to authenticated, service_role;
