-- ============================================================
-- ORPHANED PHOTO SWEEP
-- Run in the Supabase SQL Editor AFTER supabase-migration-STORAGE.sql,
-- supabase-migration-ADHOC.sql (which is where public.on_team comes from),
-- supabase-migration-REQUESTS2.sql and supabase-migration-DAILY-PICK-LIST.sql
-- — it reads every table that names a file in the bucket, so all of them have
-- to exist first. Safe to run more than once, and safe to run as one batch.
--
-- A photo is deleted in two steps: the row that names it, and the file itself.
-- Everywhere in the app those two are done together, but they cannot be one
-- transaction — the file goes through the storage API and the row through
-- postgrest — so a failure between them leaves a file nothing points at.
-- Nothing reads it, nothing lists it, and it counts against the 1 GB for ever.
-- This is the sweep that finds those and hands them back to be deleted.
-- ============================================================


-- ---------- PART 1 of 5 : how long a file is left alone ----------
-- A file is uploaded before the row that names it is written, and on a long
-- pick list the two can be minutes apart — a sweep with no grace period would
-- delete the thumbnails of a sheet that is still being taken in. A day is far
-- longer than any of those gaps and far shorter than the five-day bin.
create or replace function public.photo_grace()
returns interval language sql immutable
as $func$ select interval '1 day' $func$;


-- ---------- PART 2 of 5 : is anything still pointing at it ----------
-- Every column in the schema that holds a path in this bucket. A file is an
-- orphan only if none of them names it — miss one out and the sweep deletes
-- pictures that are still on screen, so this list is the whole safety of it.
--
--   shipment_items.photo_path   order line photos
--   shipment_boxes.photo_path   packed box photos
--   pickups.photo_path          store pick up photos
--   warehouse_skus.photo_path   the catalogue's picture of a SKU
--   adhoc_items.photo_path      points at a pick list thumbnail, not a copy
--   announcements.photo_path    the picture on an announcement
--   pick_list_items.image_path  the row thumbnails read off the PDF
--   request_photos.path         what somebody attached to a request
create or replace function public.photo_in_use(p text)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select exists (select 1 from public.shipment_items   where photo_path = p)
      or exists (select 1 from public.shipment_boxes   where photo_path = p)
      or exists (select 1 from public.pickups          where photo_path = p)
      or exists (select 1 from public.warehouse_skus   where photo_path = p)
      or exists (select 1 from public.adhoc_items      where photo_path = p)
      or exists (select 1 from public.announcements    where photo_path = p)
      or exists (select 1 from public.pick_list_items  where image_path = p)
      or exists (select 1 from public.request_photos   where path       = p);
$func$;


-- ---------- PART 3 of 5 : what is orphaned ----------
-- One anti-join rather than photo_in_use once per file: a bucket holding tens
-- of thousands of pick list thumbnails would otherwise be tens of thousands of
-- lookups through eight subqueries each. photo_in_use is still what the delete
-- rule in PART 5 asks, because that is asked of one file at a time.
create or replace function public.orphan_photos()
returns table(path text, bytes bigint)
language sql
security definer
stable
set search_path = public
as $func$
  with used as (
             select photo_path as p from public.shipment_items  where photo_path is not null
    union all select photo_path      from public.shipment_boxes where photo_path is not null
    union all select photo_path      from public.pickups        where photo_path is not null
    union all select photo_path      from public.warehouse_skus where photo_path is not null
    union all select photo_path      from public.adhoc_items    where photo_path is not null
    union all select photo_path      from public.announcements  where photo_path is not null
    union all select image_path      from public.pick_list_items where image_path is not null
    union all select path            from public.request_photos  where path       is not null
  )
  select o.name, coalesce((o.metadata->>'size')::bigint, 0)::bigint
    from storage.objects o
   where public.on_team(auth.uid())          -- an anonymous caller sweeps nothing
     and o.bucket_id = 'shipment-photos'
     and o.created_at < now() - public.photo_grace()
     and not exists (select 1 from used u where u.p = o.name);
$func$;

-- The same thing counted rather than listed, for the gauge in the menu. Kept
-- apart so opening the drawer does not send a list of every stray file to a
-- phone only to show one number.
create or replace function public.orphan_photo_usage()
returns table(files bigint, bytes bigint)
language sql
security definer
stable
set search_path = public
as $func$
  select count(*)::bigint, coalesce(sum(o.bytes), 0)::bigint
    from public.orphan_photos() o;
$func$;


-- ---------- PART 4 of 5 : twice a day, once between everybody ----------
-- The app has no server to run a cron on, so the sweep goes off when somebody
-- opens it. Whoever gets there first should be the only one who runs it, or
-- twenty phones would each scan the bucket every morning — so the clock lives
-- here rather than in any one browser, and the function decides for itself
-- whether it is due. Twelve hours apart is the twice a day this is asked for.
create table if not exists public.app_sweeps (
  kind   text primary key,
  ran_at timestamptz not null default now()
);
alter table public.app_sweeps enable row level security;

-- Readable so a screen can say when it last ran; written only by the function
-- below, which is why there is no insert or update policy.
drop policy if exists "team_select" on public.app_sweeps;
create policy "team_select" on public.app_sweeps
  for select using (public.on_team(auth.uid()));

-- Hands back the paths to delete, exactly as purge_old_pick_lists does and for
-- the same reason: deleting the row in storage.objects does not remove the
-- file, so the app has to take these to the storage API itself.
--
-- The clock is stamped before the files are gone rather than after, because
-- nothing here can know whether they went. A sweep that fails halfway leaves
-- the rest to the next one twelve hours later — which is exactly what happened
-- to the file that was orphaned in the first place.
create or replace function public.sweep_orphan_photos(force boolean default false)
returns table(path text, bytes bigint)
language plpgsql
security definer
set search_path = public
as $func$
begin
  if not public.on_team(auth.uid()) then return; end if;

  if not force and exists (select 1 from public.app_sweeps
                            where kind = 'orphan_photos'
                              and ran_at > now() - interval '12 hours')
  then return; end if;

  insert into public.app_sweeps(kind, ran_at) values ('orphan_photos', now())
    on conflict (kind) do update set ran_at = excluded.ran_at;

  return query select o.path, o.bytes from public.orphan_photos() o;
end $func$;

revoke all on function public.photo_grace()                from public;
revoke all on function public.photo_in_use(text)           from public;
revoke all on function public.orphan_photos()              from public;
revoke all on function public.orphan_photo_usage()         from public;
revoke all on function public.sweep_orphan_photos(boolean) from public;
grant execute on function public.photo_grace()                to authenticated;
grant execute on function public.photo_in_use(text)           to authenticated;
grant execute on function public.orphan_photos()              to authenticated;
grant execute on function public.orphan_photo_usage()         to authenticated;
grant execute on function public.sweep_orphan_photos(boolean) to authenticated;


-- ---------- PART 5 of 5 : being allowed to delete it ----------
-- The bucket's standing rule is "your own folder only", and an orphan is
-- usually somebody else's — left over from a box packed by whoever was on that
-- morning. This is the narrowest rule that lets the sweep work: it permits a
-- delete only of a file in this bucket, old enough to be past the grace
-- period, that no row anywhere names. A file still in use is not deletable
-- through it however it is asked for, so the worst a mistaken sweep can do is
-- be refused.
drop policy if exists "orphan_photos_team_delete" on storage.objects;
create policy "orphan_photos_team_delete" on storage.objects
  for delete using (
    bucket_id = 'shipment-photos'
    and public.on_team(auth.uid())
    and created_at < now() - public.photo_grace()
    and not public.photo_in_use(name)
  );
