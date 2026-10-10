-- Behavioral smoke test for activity tracks, run after every migration is
-- applied (see scripts/validate_migrations.sh). It uses rows it creates and
-- removes, and fails on the first violated assertion.
do $$
declare
  v_user uuid := gen_random_uuid();
  v_other uuid := gen_random_uuid();
  v_connection uuid;
  v_job uuid;
  v_activity uuid;
  v_ok boolean;
  v_row public.provider_activity_tracks;
  v_listed integer;
begin
  insert into auth.users (id, email) values (v_user, 'tracks-smoke@example.test'),
                                            (v_other, 'tracks-other@example.test');
  insert into public.provider_connections (user_id, provider, status)
  values (v_user, 'garmin', 'connected') returning id into v_connection;
  insert into public.sync_jobs (connection_id, user_id, provider, mode, state, phase,
                                attempts, lease_expires_at)
  values (v_connection, v_user, 'garmin', 'incremental', 'running', 'tracks', 1,
          now() + interval '10 minutes') returning id into v_job;
  insert into public.provider_activities (user_id, connection_id, provider,
                                          provider_activity_id, activity_type, started_at,
                                          source_updated_at)
  values (v_user, v_connection, 'garmin', 'smoke-1', 'strength_training', now(), now())
  returning id into v_activity;

  select count(*) into v_listed
  from public.list_activities_missing_tracks(v_connection, now() - interval '1 day', 5);
  assert v_listed = 1, 'activity without track must be listed';

  v_ok := public.upsert_activity_track(v_connection, v_user, v_job, 1, v_activity,
    '{"status":"unavailable","route":null,"bounds":null,"series":null,
      "heart_rate_histogram":null,"route_point_count":0,"series_point_count":0,
      "source_point_count":0}'::jsonb);
  assert v_ok, 'unavailable track with JSON nulls must be stored';
  select * into v_row from public.provider_activity_tracks where activity_id = v_activity;
  assert v_row.status = 'unavailable', 'status stored';
  assert v_row.route is null and v_row.bounds is null and v_row.series is null
         and v_row.heart_rate_histogram is null, 'JSON nulls must become SQL NULL';

  select count(*) into v_listed
  from public.list_activities_missing_tracks(v_connection, now() - interval '1 day', 5);
  assert v_listed = 0, 'fresh unavailable track must not be retried immediately';
  update public.provider_activity_tracks
  set imported_at = now() - interval '2 days' where activity_id = v_activity;
  select count(*) into v_listed
  from public.list_activities_missing_tracks(v_connection, now() - interval '1 day', 5);
  assert v_listed = 1, 'old unavailable track must be retried';

  v_ok := public.upsert_activity_track(v_connection, v_user, v_job, 1, v_activity,
    '{"status":"available","series":{"heart_rate":[120,130],"elapsed_s":[0,1]},
      "heart_rate_histogram":{"120":1,"130":1},"route_point_count":0,
      "series_point_count":2,"source_point_count":2}'::jsonb);
  assert v_ok, 'indoor track without route must be stored';
  select * into v_row from public.provider_activity_tracks where activity_id = v_activity;
  assert v_row.status = 'available' and v_row.route is null and v_row.bounds is null,
         'missing route keys stay NULL';
  assert jsonb_typeof(v_row.series) = 'object', 'series stored';

  v_ok := public.upsert_activity_track(v_connection, v_user, v_job, 1, v_activity,
    '{"status":"available","route":[[48.1,2.1],[48.2,2.2]],
      "bounds":{"min_lat":48.1,"min_lon":2.1,"max_lat":48.2,"max_lon":2.2},
      "route_point_count":2}'::jsonb);
  assert v_ok, 'track with route must be stored';
  select * into v_row from public.provider_activity_tracks where activity_id = v_activity;
  assert jsonb_array_length(v_row.route) = 2 and v_row.series is null,
         'new upsert replaces every field';

  select count(*) into v_listed
  from public.list_activities_missing_tracks(v_connection, now() - interval '1 day', 5);
  assert v_listed = 0, 'current track must not be listed';
  update public.provider_activities set source_updated_at = now() + interval '1 hour'
  where id = v_activity;
  select count(*) into v_listed
  from public.list_activities_missing_tracks(v_connection, now() - interval '1 day', 5);
  assert v_listed = 1, 'track built from an older source update must be listed';

  v_ok := public.upsert_activity_track(v_connection, v_user, v_job, 2, v_activity,
                                       '{"status":"unavailable"}'::jsonb);
  assert not v_ok, 'stale job attempt must be rejected';
  v_ok := public.upsert_activity_track(v_connection, v_other, v_job, 1, v_activity,
                                       '{"status":"unavailable"}'::jsonb);
  assert not v_ok, 'another user must be rejected';

  delete from auth.users where id in (v_user, v_other);
  assert not exists (select 1 from public.provider_activity_tracks where activity_id = v_activity),
         'tracks must cascade with the activity';
end;
$$;
