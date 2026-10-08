create table public.provider_connections (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null,
  status text not null check (status in ('disconnected','connecting','mfa_required','connected','expired','reconnect_required','rate_limited','provider_unavailable','error')),
  connected_at timestamptz,
  last_authenticated_at timestamptz,
  last_sync_at timestamptz,
  last_error_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, provider)
);

create table public.provider_secrets (
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null,
  connection_id uuid not null references public.provider_connections(id) on delete cascade,
  ciphertext text not null,
  updated_at timestamptz not null default now(),
  primary key (user_id, provider)
);

create table public.auth_challenges (
  id uuid primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null,
  encrypted_state text not null,
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
);
create index auth_challenges_expiry_idx on public.auth_challenges (expires_at);

create table public.auth_attempts (
  user_id uuid not null references auth.users(id) on delete cascade,
  window_number bigint not null,
  attempts integer not null,
  primary key (user_id, window_number)
);

create table public.sync_jobs (
  id uuid primary key default gen_random_uuid(),
  connection_id uuid not null references public.provider_connections(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null,
  mode text not null check (mode in ('historical','incremental')),
  state text not null check (state in ('queued','running','succeeded','failed')),
  phase text not null,
  cursor_date date,
  oldest_date date not null default date '2000-01-01',
  oldest_synchronized_date date,
  activity_count integer not null default 0,
  last_error_code text,
  last_success_at timestamptz,
  lease_expires_at timestamptz,
  attempts integer not null default 0,
  next_attempt_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index sync_jobs_worker_idx on public.sync_jobs (state, created_at);
create index sync_jobs_user_idx on public.sync_jobs (user_id, created_at desc);
create unique index sync_jobs_active_idx on public.sync_jobs (connection_id, mode) where state in ('queued','running');

create table public.provider_activities (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null,
  provider_activity_id text not null,
  activity_type text,
  started_at timestamptz,
  distance_meters numeric,
  duration_seconds numeric,
  source_updated_at timestamptz,
  updated_at timestamptz not null default now(),
  unique (user_id, provider, provider_activity_id)
);
create index provider_activities_user_time_idx on public.provider_activities (user_id, started_at desc);

alter table public.provider_connections enable row level security;
alter table public.provider_secrets enable row level security;
alter table public.auth_challenges enable row level security;
alter table public.auth_attempts enable row level security;
alter table public.sync_jobs enable row level security;
alter table public.provider_activities enable row level security;

create policy connections_owner_read on public.provider_connections for select to authenticated using (user_id = auth.uid());
create policy sync_jobs_owner_read on public.sync_jobs for select to authenticated using (user_id = auth.uid());
create policy activities_owner_read on public.provider_activities for select to authenticated using (user_id = auth.uid());

revoke all on public.provider_secrets, public.auth_challenges, public.auth_attempts from anon, authenticated;
grant select on public.provider_connections, public.sync_jobs, public.provider_activities to authenticated;
grant all on public.provider_connections, public.provider_secrets, public.auth_challenges, public.auth_attempts, public.sync_jobs, public.provider_activities to service_role;

create function public.consume_auth_challenge(p_user_id uuid, p_challenge_id uuid)
returns table (encrypted_state text)
language sql security definer set search_path = public
as $$
  delete from public.auth_challenges
  where id = p_challenge_id and user_id = p_user_id and provider = 'garmin' and expires_at > now()
  returning auth_challenges.encrypted_state;
$$;

create function public.allow_auth_attempt(p_user_id uuid)
returns boolean
language plpgsql security definer set search_path = public
as $$
declare current_count integer;
begin
  insert into public.auth_attempts (user_id, window_number, attempts)
  values (p_user_id, floor(extract(epoch from now()) / 600)::bigint, 1)
  on conflict (user_id, window_number)
  do update set attempts = auth_attempts.attempts + 1
  returning attempts into current_count;
  return current_count <= 5;
end;
$$;

revoke all on function public.consume_auth_challenge(uuid, uuid) from public, anon, authenticated;
revoke all on function public.allow_auth_attempt(uuid) from public, anon, authenticated;
grant execute on function public.consume_auth_challenge(uuid, uuid) to service_role;
grant execute on function public.allow_auth_attempt(uuid) to service_role;

create function public.claim_sync_job()
returns setof public.sync_jobs
language plpgsql security definer set search_path = public
as $$
begin
  return query
  update public.sync_jobs job
  set state = 'running', lease_expires_at = now() + interval '30 minutes',
      attempts = job.attempts + 1, updated_at = now()
  where job.id = (
    select candidate.id from public.sync_jobs candidate
    where (candidate.state = 'queued' and candidate.next_attempt_at <= now())
       or (candidate.state = 'running' and candidate.lease_expires_at < now())
    order by candidate.created_at
    for update skip locked limit 1
  )
  returning job.*;
end;
$$;
revoke all on function public.claim_sync_job() from public, anon, authenticated;
grant execute on function public.claim_sync_job() to service_role;

create function public.store_provider_secret(p_user_id uuid, p_provider text,
  p_connection_id uuid, p_ciphertext text)
returns boolean
language plpgsql security definer set search_path = public
as $$
declare stored boolean;
begin
  if not exists (
    select 1 from public.provider_connections
    where id = p_connection_id and user_id = p_user_id and provider = p_provider
  ) then
    return false;
  end if;
  insert into public.provider_secrets (user_id, provider, connection_id, ciphertext)
  values (p_user_id, p_provider, p_connection_id, p_ciphertext)
  on conflict (user_id, provider) do update
  set ciphertext = excluded.ciphertext, updated_at = now()
  where provider_secrets.connection_id = excluded.connection_id
  returning true into stored;
  return coalesce(stored, false);
end;
$$;
revoke all on function public.store_provider_secret(uuid, text, uuid, text) from public, anon, authenticated;
grant execute on function public.store_provider_secret(uuid, text, uuid, text) to service_role;
