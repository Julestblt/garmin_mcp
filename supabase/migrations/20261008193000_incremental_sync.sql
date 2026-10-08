create index provider_connections_connected_idx
  on public.provider_connections (id) where status = 'connected';

create function public.schedule_incremental_sync()
returns integer
language plpgsql security definer set search_path = public
as $$
declare scheduled integer;
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
  return scheduled;
end;
$$;
revoke all on function public.schedule_incremental_sync() from public, anon, authenticated;
grant execute on function public.schedule_incremental_sync() to service_role;
