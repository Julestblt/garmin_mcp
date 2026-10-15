alter table public.provider_connections
  add column manual_sync_available_at timestamptz;

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
         greatest(historical.oldest_date, current_date - case
           when historical.phase <> 'activities' then 7
           when exists (
             select 1 from public.sync_jobs catchup
             where catchup.connection_id = connection.id and catchup.mode = 'incremental'
               and catchup.phase = 'activities'
               and catchup.created_at > now() - interval '6 hours'
               and catchup.oldest_date <= greatest(historical.oldest_date,
                                                   catchup.created_at::date - 14)
           ) then 3
           else 14 end)
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
             or recent.created_at > now() - case when historical.phase = 'activities'
                                                 then interval '50 minutes'
                                                 else interval '6 hours' end)
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

create function public.request_manual_sync(p_user_id uuid)
returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  target public.provider_connections%rowtype;
  activity_floor date;
begin
  select * into target from public.provider_connections
  where user_id = p_user_id and provider = 'garmin' and status <> 'disconnected'
  for update;
  if not found then
    return jsonb_build_object('result', 'not_connected');
  end if;
  if target.status in ('reconnect_required', 'expired') then
    return jsonb_build_object('result', 'reconnect_required');
  end if;
  if target.status <> 'connected' then
    return jsonb_build_object('result', 'not_connected');
  end if;
  if exists (
    select 1 from public.sync_jobs active
    where active.connection_id = target.id and active.mode = 'incremental'
      and active.phase = 'activities' and active.state in ('queued', 'running')
  ) then
    return jsonb_build_object('result', 'already_running',
                              'available_at', target.manual_sync_available_at);
  end if;
  if target.manual_sync_available_at > now() then
    return jsonb_build_object('result', 'throttled',
                              'available_at', target.manual_sync_available_at,
                              'retry_after_seconds', greatest(1, ceil(extract(epoch from
                                target.manual_sync_available_at - now())))::integer);
  end if;
  select historical.oldest_date into activity_floor from public.sync_jobs historical
  where historical.connection_id = target.id and historical.mode = 'historical'
    and historical.phase = 'activities'
  order by historical.created_at desc limit 1;
  insert into public.sync_jobs (
    connection_id, user_id, provider, mode, state, phase, cursor_date, oldest_date,
    next_attempt_at
  )
  values
    (target.id, target.user_id, 'garmin', 'incremental', 'queued', 'activities',
     current_date, greatest(coalesce(activity_floor, current_date - 3), current_date - 3),
     now()),
    (target.id, target.user_id, 'garmin', 'incremental', 'queued', 'recovery',
     current_date, current_date, now()),
    (target.id, target.user_id, 'garmin', 'incremental', 'queued', 'tracks',
     current_date, current_date, now() + interval '1 minute')
  on conflict do nothing;
  update public.provider_connections
  set manual_sync_available_at = now() + interval '5 minutes', updated_at = now()
  where id = target.id
  returning manual_sync_available_at into target.manual_sync_available_at;
  return jsonb_build_object('result', 'queued',
                            'available_at', target.manual_sync_available_at);
end;
$$;
revoke all on function public.request_manual_sync(uuid) from public, anon, authenticated;
grant execute on function public.request_manual_sync(uuid) to service_role;
