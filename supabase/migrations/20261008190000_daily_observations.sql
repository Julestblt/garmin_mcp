create table public.provider_observations (
  id uuid primary key default gen_random_uuid(),
  connection_id uuid not null references public.provider_connections(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null,
  domain text not null check (domain in ('recovery', 'fitness')),
  metric text not null,
  observed_on date not null,
  observed_at timestamptz,
  value_numeric numeric,
  value_text text,
  unit text,
  availability text not null check (availability in (
    'available', 'missing', 'unsupported', 'unavailable_historically', 'partial', 'stale'
  )),
  source_updated_at timestamptz,
  imported_at timestamptz not null default now(),
  unique (user_id, provider, domain, metric, observed_on)
);
create index provider_observations_user_date_idx
  on public.provider_observations (user_id, domain, observed_on desc);
create index provider_observations_connection_idx
  on public.provider_observations (connection_id, domain);
alter table public.provider_observations enable row level security;
create policy observations_owner_read on public.provider_observations
  for select to authenticated using (user_id = auth.uid());
grant select on public.provider_observations to authenticated;
grant all on public.provider_observations to service_role;

drop index public.sync_jobs_active_idx;
create unique index sync_jobs_active_idx on public.sync_jobs (connection_id, mode, phase)
  where state in ('queued', 'running');
create index sync_jobs_claim_idx on public.sync_jobs (next_attempt_at, created_at)
  where state in ('queued', 'running');

create or replace function public.claim_sync_job()
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
    join public.provider_connections connection on connection.id = candidate.connection_id
    where connection.status = 'connected'
      and ((candidate.state = 'queued' and candidate.next_attempt_at <= now())
        or (candidate.state = 'running' and candidate.lease_expires_at < now()))
    order by candidate.updated_at, candidate.created_at
    for update of candidate skip locked limit 1
  )
  returning job.*;
end;
$$;

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
     greatest(p_oldest_date, current_date - 365));
  delete from public.auth_challenges
  where user_id = p_user_id and provider = 'garmin';
  return new_id;
end;
$$;

insert into public.sync_jobs (connection_id, user_id, provider, mode, state, phase, oldest_date)
select connection.id, connection.user_id, connection.provider, 'historical', 'queued',
       domains.phase, greatest(current_date - domains.days, coalesce(activity.oldest_date, current_date))
from public.provider_connections connection
cross join (values ('recovery', 90), ('fitness', 365)) as domains(phase, days)
left join lateral (
  select oldest_date from public.sync_jobs
  where connection_id = connection.id and phase = 'activities'
  order by created_at desc limit 1
) activity on true
where connection.provider = 'garmin' and connection.status = 'connected'
  and not exists (
    select 1 from public.sync_jobs existing
    where existing.connection_id = connection.id and existing.phase = domains.phase
  );

create function public.upsert_provider_observations(
  p_connection_id uuid, p_user_id uuid, p_job_id uuid, p_attempts integer,
  p_observations jsonb
)
returns boolean
language plpgsql security definer set search_path = public
as $$
declare observation jsonb;
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
  for observation in select value from jsonb_array_elements(p_observations)
  loop
    insert into public.provider_observations (
      connection_id, user_id, provider, domain, metric, observed_on, observed_at,
      value_numeric, value_text, unit, availability, source_updated_at
    ) values (
      p_connection_id, p_user_id, 'garmin', observation->>'domain',
      observation->>'metric', (observation->>'observed_on')::date,
      (observation->>'observed_at')::timestamptz,
      (observation->>'value_numeric')::numeric,
      observation->>'value_text', observation->>'unit',
      observation->>'availability',
      (observation->>'source_updated_at')::timestamptz
    ) on conflict (user_id, provider, domain, metric, observed_on)
    do update set
      connection_id = excluded.connection_id,
      observed_at = excluded.observed_at,
      value_numeric = excluded.value_numeric,
      value_text = excluded.value_text,
      unit = excluded.unit,
      availability = excluded.availability,
      source_updated_at = excluded.source_updated_at,
      imported_at = now();
  end loop;
  return true;
end;
$$;
revoke all on function public.upsert_provider_observations(uuid, uuid, uuid, integer, jsonb)
  from public, anon, authenticated;
grant execute on function public.upsert_provider_observations(uuid, uuid, uuid, integer, jsonb)
  to service_role;
