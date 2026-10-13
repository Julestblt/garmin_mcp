alter table public.provider_observations
  drop constraint provider_observations_domain_check;
alter table public.provider_observations
  add constraint provider_observations_domain_check
  check (domain in ('recovery', 'fitness', 'thresholds'));

create table public.provider_heart_rate_zones (
  connection_id uuid not null references public.provider_connections(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null,
  sport text not null,
  training_method text,
  zone1_floor_bpm integer,
  zone2_floor_bpm integer,
  zone3_floor_bpm integer,
  zone4_floor_bpm integer,
  zone5_floor_bpm integer,
  max_heart_rate_bpm integer,
  resting_heart_rate_bpm integer,
  lactate_threshold_heart_rate_bpm integer,
  resting_heart_rate_auto_update boolean,
  imported_at timestamptz not null default now(),
  primary key (user_id, provider, sport)
);
create index provider_heart_rate_zones_connection_idx
  on public.provider_heart_rate_zones (connection_id);
alter table public.provider_heart_rate_zones enable row level security;
create policy heart_rate_zones_owner_read on public.provider_heart_rate_zones
  for select to authenticated using (user_id = (select auth.uid()));
grant select on public.provider_heart_rate_zones to authenticated;
grant all on public.provider_heart_rate_zones to service_role;

create function public.replace_heart_rate_zones(
  p_connection_id uuid, p_user_id uuid, p_job_id uuid, p_attempts integer,
  p_zones jsonb
)
returns boolean
language plpgsql security definer set search_path = public
as $$
declare zone jsonb;
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
  delete from public.provider_heart_rate_zones
  where user_id = p_user_id and provider = 'garmin';
  for zone in select value from jsonb_array_elements(p_zones)
  loop
    insert into public.provider_heart_rate_zones (
      connection_id, user_id, provider, sport, training_method,
      zone1_floor_bpm, zone2_floor_bpm, zone3_floor_bpm, zone4_floor_bpm, zone5_floor_bpm,
      max_heart_rate_bpm, resting_heart_rate_bpm, lactate_threshold_heart_rate_bpm,
      resting_heart_rate_auto_update
    ) values (
      p_connection_id, p_user_id, 'garmin', zone->>'sport', zone->>'training_method',
      (zone->>'zone1_floor_bpm')::integer, (zone->>'zone2_floor_bpm')::integer,
      (zone->>'zone3_floor_bpm')::integer, (zone->>'zone4_floor_bpm')::integer,
      (zone->>'zone5_floor_bpm')::integer, (zone->>'max_heart_rate_bpm')::integer,
      (zone->>'resting_heart_rate_bpm')::integer,
      (zone->>'lactate_threshold_heart_rate_bpm')::integer,
      (zone->>'resting_heart_rate_auto_update')::boolean
    );
  end loop;
  return true;
end;
$$;
revoke all on function public.replace_heart_rate_zones(uuid, uuid, uuid, integer, jsonb)
  from public, anon, authenticated;
grant execute on function public.replace_heart_rate_zones(uuid, uuid, uuid, integer, jsonb)
  to service_role;

create or replace function public.activate_garmin_connection(
  p_user_id uuid, p_expected_connection_id uuid,
  p_ciphertext text, p_oldest_date date
)
returns uuid
language plpgsql security definer set search_path = public
as $$
declare current_id uuid;
declare new_id uuid;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':garmin', 0));
  select id into current_id from public.provider_connections
  where user_id = p_user_id and provider = 'garmin' and status <> 'disconnected'
  for update;
  if current_id is distinct from p_expected_connection_id then
    return null;
  end if;
  if current_id is not null then
    update public.provider_connections
    set status = 'disconnected', lease_owner = null, lease_expires_at = null,
        updated_at = now()
    where id = current_id;
    update public.sync_jobs
    set state = 'failed', last_error_code = 'connection_replaced',
        lease_expires_at = null, updated_at = now()
    where connection_id = current_id and state in ('queued', 'running');
  end if;
  delete from public.provider_secrets
  where user_id = p_user_id and provider = 'garmin';
  insert into public.provider_connections (
    user_id, provider, status, connected_at, last_authenticated_at
  ) values (p_user_id, 'garmin', 'connected', now(), now())
  returning id into new_id;
  insert into public.provider_secrets (
    user_id, provider, connection_id, ciphertext
  ) values (p_user_id, 'garmin', new_id, p_ciphertext);
  insert into public.sync_jobs (
    connection_id, user_id, provider, mode, state, phase, oldest_date
  ) values
    (new_id, p_user_id, 'garmin', 'historical', 'queued', 'activities', p_oldest_date),
    (new_id, p_user_id, 'garmin', 'historical', 'queued', 'recovery',
     greatest(p_oldest_date, current_date - 90)),
    (new_id, p_user_id, 'garmin', 'historical', 'queued', 'fitness',
     greatest(p_oldest_date, current_date - 365)),
    (new_id, p_user_id, 'garmin', 'historical', 'queued', 'thresholds',
     greatest(p_oldest_date, current_date - 364));
  delete from public.auth_challenges
  where user_id = p_user_id and provider = 'garmin';
  return new_id;
end;
$$;

create or replace function public.schedule_incremental_sync()
returns integer
language plpgsql security definer set search_path = public
as $$
declare scheduled integer;
declare scheduled_tracks integer;
declare scheduled_thresholds integer;
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
  insert into public.sync_jobs (
    connection_id, user_id, provider, mode, state, phase, cursor_date, oldest_date
  )
  select connection.id, connection.user_id, 'garmin', 'incremental', 'queued',
         'thresholds', current_date, greatest(historical.oldest_date, current_date - 30)
  from public.provider_connections connection
  join public.sync_jobs historical on historical.connection_id = connection.id
    and historical.mode = 'historical' and historical.state = 'succeeded'
    and historical.phase = 'thresholds'
  where connection.provider = 'garmin' and connection.status = 'connected'
    and not exists (
      select 1 from public.sync_jobs recent
      where recent.connection_id = connection.id and recent.mode = 'incremental'
        and recent.phase = 'thresholds'
        and (recent.state in ('queued', 'running')
             or recent.created_at > now() - interval '24 hours')
    )
  on conflict do nothing;
  get diagnostics scheduled_thresholds = row_count;
  return scheduled + scheduled_tracks + scheduled_thresholds;
end;
$$;

insert into public.sync_jobs (connection_id, user_id, provider, mode, state, phase, oldest_date)
select connection.id, connection.user_id, connection.provider, 'historical', 'queued',
       'thresholds', current_date - 364
from public.provider_connections connection
where connection.provider = 'garmin' and connection.status = 'connected'
  and not exists (
    select 1 from public.sync_jobs existing
    where existing.connection_id = connection.id and existing.phase = 'thresholds'
  );
