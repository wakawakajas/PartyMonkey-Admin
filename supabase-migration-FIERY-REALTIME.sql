-- The Fiery watcher on the shop PC now waits for Supabase to tell it a job was
-- queued, instead of asking every 4 seconds. That needs fiery_jobs in the
-- realtime publication (label_jobs already is, from SEED-LABELS).
-- Safe to run twice.
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public' and tablename = 'fiery_jobs'
  ) then
    alter publication supabase_realtime add table public.fiery_jobs;
  end if;
end $$;
