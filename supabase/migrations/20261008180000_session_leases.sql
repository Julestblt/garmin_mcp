alter table public.provider_connections
  add column lease_owner uuid,
  add column lease_expires_at timestamptz;

alter table public.provider_secrets
  add column version bigint not null default 1;

create function public.acquire_provider_lease(p_connection_id uuid, p_owner uuid)
returns boolean
language plpgsql security definer set search_path = public
as $$
declare acquired boolean;
begin
  update public.provider_connections
  set lease_owner = p_owner, lease_expires_at = now() + interval '2 minutes'
  where id = p_connection_id and status = 'connected'
    and (lease_owner is null or lease_expires_at < now())
  returning true into acquired;
  return coalesce(acquired, false);
end;
$$;

create function public.renew_provider_lease(p_connection_id uuid, p_owner uuid)
returns boolean
language plpgsql security definer set search_path = public
as $$
declare renewed boolean;
begin
  update public.provider_connections
  set lease_expires_at = now() + interval '2 minutes'
  where id = p_connection_id and lease_owner = p_owner
  returning true into renewed;
  return coalesce(renewed, false);
end;
$$;

create function public.release_provider_lease(p_connection_id uuid, p_owner uuid)
returns void
language sql security definer set search_path = public
as $$
  update public.provider_connections
  set lease_owner = null, lease_expires_at = null
  where id = p_connection_id and lease_owner = p_owner;
$$;

create function public.persist_provider_secret(
  p_user_id uuid, p_connection_id uuid, p_owner uuid,
  p_version bigint, p_ciphertext text
)
returns bigint
language plpgsql security definer set search_path = public
as $$
declare next_version bigint;
begin
  update public.provider_secrets secret
  set ciphertext = p_ciphertext, version = secret.version + 1, updated_at = now()
  where secret.user_id = p_user_id and secret.provider = 'garmin'
    and secret.connection_id = p_connection_id and secret.version = p_version
    and exists (
      select 1 from public.provider_connections connection
      where connection.id = p_connection_id and connection.user_id = p_user_id
        and connection.provider = 'garmin' and connection.status = 'connected'
        and connection.lease_owner = p_owner and connection.lease_expires_at > now()
    )
  returning secret.version into next_version;
  return next_version;
end;
$$;

revoke all on function public.acquire_provider_lease(uuid, uuid) from public, anon, authenticated;
revoke all on function public.renew_provider_lease(uuid, uuid) from public, anon, authenticated;
revoke all on function public.release_provider_lease(uuid, uuid) from public, anon, authenticated;
revoke all on function public.persist_provider_secret(uuid, uuid, uuid, bigint, text) from public, anon, authenticated;
grant execute on function public.acquire_provider_lease(uuid, uuid) to service_role;
grant execute on function public.renew_provider_lease(uuid, uuid) to service_role;
grant execute on function public.release_provider_lease(uuid, uuid) to service_role;
grant execute on function public.persist_provider_secret(uuid, uuid, uuid, bigint, text) to service_role;
