-- Magic Create hardening. Safe to run more than once.
--
-- 1. The three-day purge returned split-page artwork as one JSON string instead of
--    the file paths inside it, so those page files were never removed from storage.
--    It also now returns the copy a one-off gift tag design keeps (sheet_item.oneOff).
-- 2. purge_old_fiery_jobs() could be called by anybody holding the anon key.

-- ---------- 1. purge: unroll split pages and one-off copies ----------
create or replace function public.purge_old_magic_batches()
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

  select array_agg(b.id) into doomed
    from public.magic_batches b
   where b.status = 'completed'
     and b.completed_at is not null
     and b.completed_at < now() - interval '3 days';

  if doomed is null then return; end if;

  -- art is { "front": "<path>", "back": "<path>", "frontSplit": [{"path": ...}], ... }
  -- either side possibly absent, so strings are taken as paths and the split
  -- lists are unrolled into the paths inside them
  return query
    select v.value #>> '{}'
      from public.magic_items i, jsonb_each(i.art) as v
     where i.batch_id = any(doomed)
       and jsonb_typeof(v.value) = 'string'
       and (v.value #>> '{}') <> ''
    union
    select e ->> 'path'
      from public.magic_items i, jsonb_each(i.art) as v, jsonb_array_elements(v.value) as e
     where i.batch_id = any(doomed)
       and jsonb_typeof(v.value) = 'array'
       and coalesce(e ->> 'path', '') <> ''
    union
    select i.sheet_item #>> '{oneOff,path}'
      from public.magic_items i
     where i.batch_id = any(doomed)
       and coalesce(i.sheet_item #>> '{oneOff,path}', '') <> '';

  -- magic_items goes with it on the cascade
  delete from public.magic_batches where id = any(doomed);
end $func$;

revoke all on function public.purge_old_magic_batches() from public;
grant execute on function public.purge_old_magic_batches() to authenticated;

-- ---------- 2. fiery purge: team only ----------
create or replace function public.purge_old_fiery_jobs()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if not public.on_team(auth.uid()) then return; end if;
  delete from public.fiery_jobs
   where status in ('sent','failed')
     and created_at < now() - interval '2 days';
end $$;

revoke all on function public.purge_old_fiery_jobs() from public;
grant execute on function public.purge_old_fiery_jobs() to authenticated;
