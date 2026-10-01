-- The pickup sync on the shop PC now waits for Supabase to tell it a button was
-- pressed (From BigSeller, or the ship step) instead of asking every 4 seconds.
-- That needs pickup_sync_jobs in the realtime publication. Safe to run twice.
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public' and tablename = 'pickup_sync_jobs'
  ) then
    alter publication supabase_realtime add table public.pickup_sync_jobs;
  end if;
end $$;
