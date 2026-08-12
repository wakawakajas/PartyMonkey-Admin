-- ============================================================
-- STORAGE GAUGE — run in the Supabase SQL Editor.
-- Safe to run more than once.
--
-- Totalling the bucket from the app would mean listing every file, a hundred
-- at a time. One query over storage.objects does it instead, behind a
-- security definer function because that schema is not reachable directly.
-- ============================================================

create or replace function public.storage_usage()
returns table(files bigint, bytes bigint)
language sql
security definer
stable
set search_path = public
as $func$
  select count(*)::bigint,
         coalesce(sum((metadata->>'size')::bigint), 0)::bigint
    from storage.objects
   where bucket_id = 'shipment-photos';
$func$;

revoke all on function public.storage_usage() from public;
grant execute on function public.storage_usage() to authenticated;
