-- Minimal Supabase-compatible bootstrap for validating migrations against
-- a disposable PostgreSQL instance (CI or local). It creates only the
-- roles, schema, and auth helpers the Stride migrations reference. It does
-- not reproduce Supabase Auth behavior; it exists so every migration can be
-- applied in order and parsed by PostgreSQL.
create extension if not exists pgcrypto;

do $$
begin
  create role anon nologin;
exception when duplicate_object then null;
end $$;

do $$
begin
  create role authenticated nologin;
exception when duplicate_object then null;
end $$;

do $$
begin
  create role service_role nologin;
exception when duplicate_object then null;
end $$;

create schema if not exists auth;

create table if not exists auth.users (
  id uuid primary key default gen_random_uuid(),
  email text
);

create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
$$;
