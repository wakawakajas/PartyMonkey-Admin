-- ============================================================
-- DAILY PICK LIST and BUNDLE SKU — completed work goes in two days
-- Run in the Supabase SQL Editor AFTER supabase-migration-DAILY-PICK-LIST.sql
-- and supabase-migration-BUNDLE-ORDERS.sql.
-- Safe to run more than once. Plain statements, no DO blocks.
--
-- A finished sheet was kept five days and a finished order three. Both are
-- looked at the morning after and never again, so both are now two: long
-- enough to check what was picked or packed, short enough that the bucket
-- and the list stay the size of this week's work.
--
-- Magic Create batches and Requests keep their own three days -- they are
-- not this, and are not touched here.
-- ============================================================

-- ---------- the pick list ----------
-- Unchanged but for the interval: it still hands back the storage paths it
-- removed, because deleting a row in pick_list_items does not remove the file.
create or replace function public.purge_old_pick_lists()
returns table(path text)
language plpgsql
security definer
set search_path = public
as $func$
declare
  doomed uuid[];
begin
  -- an anonymous caller cleans up nothing
  if not public.on_team(auth.uid()) then return; end if;

  select array_agg(l.id) into doomed
    from public.pick_lists l
   where l.status = 'completed'
     and l.completed_at is not null
     and l.completed_at < now() - interval '2 days';

  if doomed is null then return; end if;

  return query
    select i.image_path from public.pick_list_items i
     where i.list_id = any(doomed) and i.image_path is not null;

  -- pick_list_items goes with it on the cascade
  delete from public.pick_lists where id = any(doomed);
end $func$;

revoke all on function public.purge_old_pick_lists() from public;
grant execute on function public.purge_old_pick_lists() to authenticated;


-- ---------- the day's orders, and the bundles under them ----------
-- An order takes its bundles with it on the cascade. A job with no order
-- above it -- one from before orders existed -- is swept on its own date.
create or replace function public.purge_old_bundle_jobs()
returns void
language plpgsql
security definer
set search_path = public
as $func$
begin
  -- an anonymous caller cleans up nothing
  if not public.on_team(auth.uid()) then return; end if;

  delete from public.bundle_orders
   where status = 'completed'
     and completed_at is not null
     and completed_at < now() - interval '2 days';

  delete from public.bundle_jobs
   where order_id is null
     and status = 'completed'
     and completed_at is not null
     and completed_at < now() - interval '2 days';
end $func$;

revoke all on function public.purge_old_bundle_jobs() from public;
grant execute on function public.purge_old_bundle_jobs() to authenticated;
