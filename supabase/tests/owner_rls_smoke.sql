begin;

do $$
declare unexpected text;
begin
  select string_agg(tablename || '.' || policyname, ', ') into unexpected
  from pg_policies
  where schemaname = 'public'
    and qual like '%auth.uid()%'
    and qual not like '%SELECT auth.uid()%';
  if unexpected is not null then
    raise exception 'Unoptimized owner policies: %', unexpected;
  end if;

  if (select count(*) from pg_policies
      where schemaname = 'public' and policyname in (
        'connections_owner_read', 'sync_jobs_owner_read', 'activities_owner_read',
        'activity_segments_owner_read', 'observations_owner_read',
        'activity_tracks_owner_read', 'heart_rate_zones_owner_read')) <> 7 then
    raise exception 'An expected owner policy is missing';
  end if;
end $$;

insert into auth.users (id) values
  ('11111111-1111-4111-8111-111111111111'),
  ('22222222-2222-4222-8222-222222222222');
insert into public.provider_connections (user_id, provider, status) values
  ('11111111-1111-4111-8111-111111111111', 'garmin', 'connected'),
  ('22222222-2222-4222-8222-222222222222', 'garmin', 'connected');

set local role authenticated;
select set_config('request.jwt.claim.sub', '11111111-1111-4111-8111-111111111111', true);
do $$
begin
  if (select count(*) from public.provider_connections) <> 1 then
    raise exception 'Owner isolation failed';
  end if;
end $$;

rollback;
