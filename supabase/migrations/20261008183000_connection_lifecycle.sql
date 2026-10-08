alter table public.provider_connections
  drop constraint provider_connections_user_id_provider_key;

create unique index provider_connections_active_idx
  on public.provider_connections (user_id, provider)
  where status <> 'disconnected';

create index provider_connections_latest_idx
  on public.provider_connections (user_id, provider, created_at desc);

create function public.activate_garmin_connection(
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
  ) values (new_id, p_user_id, 'garmin', 'historical', 'queued', 'activities', p_oldest_date);
  delete from public.auth_challenges
  where user_id = p_user_id and provider = 'garmin';
  return new_id;
end;
$$;

create function public.disconnect_garmin_connection(p_user_id uuid)
returns boolean
language plpgsql security definer set search_path = public
as $$
declare current_id uuid;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':garmin', 0));
  select id into current_id from public.provider_connections
  where user_id = p_user_id and provider = 'garmin' and status <> 'disconnected'
  for update;
  if current_id is not null then
    update public.provider_connections
    set status = 'disconnected', lease_owner = null, lease_expires_at = null,
        updated_at = now()
    where id = current_id;
    update public.sync_jobs
    set state = 'failed', last_error_code = 'disconnected',
        lease_expires_at = null, updated_at = now()
    where connection_id = current_id and state in ('queued', 'running');
  end if;
  delete from public.provider_secrets
  where user_id = p_user_id and provider = 'garmin';
  delete from public.auth_challenges
  where user_id = p_user_id and provider = 'garmin';
  return current_id is not null;
end;
$$;

create function public.delete_garmin_data(p_user_id uuid)
returns void
language plpgsql security definer set search_path = public
as $$
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':garmin', 0));
  delete from public.auth_challenges
  where user_id = p_user_id and provider = 'garmin';
  delete from public.provider_connections
  where user_id = p_user_id and provider = 'garmin';
end;
$$;

revoke all on function public.activate_garmin_connection(uuid, uuid, text, date) from public, anon, authenticated;
revoke all on function public.disconnect_garmin_connection(uuid) from public, anon, authenticated;
revoke all on function public.delete_garmin_data(uuid) from public, anon, authenticated;
grant execute on function public.activate_garmin_connection(uuid, uuid, text, date) to service_role;
grant execute on function public.disconnect_garmin_connection(uuid) to service_role;
grant execute on function public.delete_garmin_data(uuid) to service_role;
