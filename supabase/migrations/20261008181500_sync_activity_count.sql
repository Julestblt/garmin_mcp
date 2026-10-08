create function public.count_provider_activities(p_connection_id uuid)
returns bigint
language sql security definer set search_path = public
as $$
  select count(*) from public.provider_activities
  where connection_id = p_connection_id;
$$;

revoke all on function public.count_provider_activities(uuid) from public, anon, authenticated;
grant execute on function public.count_provider_activities(uuid) to service_role;
