-- Behavioral smoke test for heart-rate zones and threshold observations, run
-- after every migration is applied (see scripts/validate_migrations.sh).
begin;

do $$
declare
  v_user uuid := '33333333-3333-4333-8333-333333333333';
  v_other uuid := '44444444-4444-4444-8444-444444444444';
  v_connection uuid;
  v_other_connection uuid;
  v_job uuid;
  v_ok boolean;
  v_count integer;
begin
  insert into auth.users (id, email) values (v_user, 'thresholds-smoke@example.test'),
                                            (v_other, 'thresholds-other@example.test');
  insert into public.provider_connections (user_id, provider, status)
  values (v_user, 'garmin', 'connected') returning id into v_connection;
  insert into public.provider_connections (user_id, provider, status)
  values (v_other, 'garmin', 'connected') returning id into v_other_connection;
  insert into public.sync_jobs (connection_id, user_id, provider, mode, state, phase,
                                oldest_date, attempts, lease_expires_at)
  values (v_connection, v_user, 'garmin', 'historical', 'running', 'thresholds',
          current_date - 364, 1, now() + interval '10 minutes') returning id into v_job;

  v_ok := public.replace_heart_rate_zones(v_connection, v_user, v_job, 1,
    '[{"sport":"DEFAULT","training_method":"HR_MAX","zone1_floor_bpm":98,
       "zone5_floor_bpm":176,"max_heart_rate_bpm":196,"resting_heart_rate_auto_update":true},
      {"sport":"RUNNING","lactate_threshold_heart_rate_bpm":171,"zone1_floor_bpm":null}]'::jsonb);
  assert v_ok, 'zones must be stored under a valid lease';
  select count(*) into v_count from public.provider_heart_rate_zones where user_id = v_user;
  assert v_count = 2, 'both sport profiles stored';
  assert (select zone1_floor_bpm from public.provider_heart_rate_zones
          where user_id = v_user and sport = 'RUNNING') is null, 'missing floor stays NULL';

  v_ok := public.replace_heart_rate_zones(v_connection, v_user, v_job, 1,
    '[{"sport":"RUNNING","lactate_threshold_heart_rate_bpm":172}]'::jsonb);
  assert v_ok, 'zones replaced';
  select count(*) into v_count from public.provider_heart_rate_zones where user_id = v_user;
  assert v_count = 1, 'profiles removed in Garmin are removed here';

  v_ok := public.replace_heart_rate_zones(v_connection, v_user, v_job, 2, '[]'::jsonb);
  assert not v_ok, 'a stale attempt must not write';
  v_ok := public.replace_heart_rate_zones(v_other_connection, v_user, v_job, 1, '[]'::jsonb);
  assert not v_ok, 'another connection must not write';

  v_ok := public.upsert_provider_observations(v_connection, v_user, v_job, 1,
    '[{"domain":"thresholds","metric":"lactate_threshold_speed_mps",
       "observed_on":"2026-08-21","value_numeric":3.8,"unit":"m/s",
       "availability":"available"}]'::jsonb);
  assert v_ok, 'threshold observations accepted';
  assert (select value_numeric from public.provider_observations
          where user_id = v_user and domain = 'thresholds') = 3.8, 'threshold stored';

  update public.sync_jobs set state = 'succeeded', lease_expires_at = null where id = v_job;
  perform public.schedule_incremental_sync();
  select count(*) into v_count from public.sync_jobs
  where connection_id = v_connection and mode = 'incremental' and phase = 'thresholds';
  assert v_count = 1, 'incremental thresholds job scheduled';
  perform public.schedule_incremental_sync();
  select count(*) into v_count from public.sync_jobs
  where connection_id = v_connection and mode = 'incremental' and phase = 'thresholds';
  assert v_count = 1, 'incremental thresholds job scheduled at most once a day';
end $$;

set local role authenticated;
select set_config('request.jwt.claim.sub', '44444444-4444-4444-8444-444444444444', true);
do $$
begin
  assert (select count(*) from public.provider_heart_rate_zones) = 0,
         'another athlete cannot read zones';
end $$;
select set_config('request.jwt.claim.sub', '33333333-3333-4333-8333-333333333333', true);
do $$
begin
  assert (select count(*) from public.provider_heart_rate_zones) = 1, 'owner reads zones';
end $$;
reset role;

do $$
begin
  perform public.delete_garmin_data('33333333-3333-4333-8333-333333333333');
  assert (select count(*) from public.provider_heart_rate_zones
          where user_id = '33333333-3333-4333-8333-333333333333') = 0,
         'data deletion removes zones';
  assert (select count(*) from public.provider_observations
          where user_id = '33333333-3333-4333-8333-333333333333') = 0,
         'data deletion removes threshold observations';
end $$;

rollback;
