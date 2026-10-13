drop policy connections_owner_read on public.provider_connections;
create policy connections_owner_read on public.provider_connections
  for select to authenticated using (user_id = (select auth.uid()));

drop policy sync_jobs_owner_read on public.sync_jobs;
create policy sync_jobs_owner_read on public.sync_jobs
  for select to authenticated using (user_id = (select auth.uid()));

drop policy activities_owner_read on public.provider_activities;
create policy activities_owner_read on public.provider_activities
  for select to authenticated using (user_id = (select auth.uid()));

drop policy activity_segments_owner_read on public.provider_activity_segments;
create policy activity_segments_owner_read on public.provider_activity_segments
  for select to authenticated using (user_id = (select auth.uid()));

drop policy observations_owner_read on public.provider_observations;
create policy observations_owner_read on public.provider_observations
  for select to authenticated using (user_id = (select auth.uid()));
