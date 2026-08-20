-- ============================================================
-- ORPHANED PHOTO SWEEP
-- Run in the Supabase SQL Editor AFTER supabase-migration-STORAGE.sql and
-- supabase-migration-ADHOC.sql (which is where public.on_team comes from).
-- Safe to run more than once, and safe to run as one batch.
--
-- A photo is deleted in two steps: the row that names it, and the file itself.
-- Everywhere in the app those two are done together, but they cannot be one
-- transaction — the file goes through the storage API and the row through
-- postgrest — so a failure between them leaves a file nothing points at.
-- Nothing reads it, nothing lists it, and it counts against the 1 GB for ever.
-- This is the sweep that finds those and hands them back to be deleted.
--
-- Nothing here names a table. What counts as "still in use" is read out of the
-- catalogue every time it is asked, so a page built next year is covered by
-- the sweep the day it is written rather than the day somebody remembers this
-- file exists — see PART 2, which is the whole safety of it.
-- ============================================================


-- ---------- PART 1 of 7 : how long a file is left alone ----------
-- A file is uploaded before the row that names it is written, and on a long
-- pick list the two can be minutes apart — a sweep with no grace period would
-- delete the thumbnails of a sheet that is still being taken in. A day is far
-- longer than any of those gaps and far shorter than the five-day bin.
create or replace function public.photo_grace()
returns interval language sql immutable
as $func$ select interval '1 day' $func$;


-- ---------- PART 2 of 7 : which columns name a file ----------
-- The dangerous mistake this sweep can make is not knowing about a column, so
-- it is never told: it asks the catalogue for every text column in public
-- whose name is `path` or ends in `_path`, which is what every one of them has
-- been called since the first — photo_path, image_path, path. A page added
-- later that follows the same habit is covered without touching this file.
--
-- Guessing wide is the safe direction. A column caught here that holds
-- something other than a file name only makes the sweep more cautious: the
-- worst it can do is leave a stray file alone. A column missed does the
-- opposite, and deletes a picture somebody is still looking at.
--
-- For a future column that breaks the habit — `cover_image`, say — add a row:
--   insert into public.photo_path_extra(tbl, col) values ('posters','cover_image');
create table if not exists public.photo_path_extra (
  tbl text not null,
  col text not null,
  primary key (tbl, col)
);
alter table public.photo_path_extra enable row level security;
drop policy if exists "team_select" on public.photo_path_extra;
create policy "team_select" on public.photo_path_extra
  for select using (public.on_team(auth.uid()));

-- Tables only, not views: a view over a table would have the sweep read the
-- same column twice and call it two different places.
create or replace function public.photo_columns()
returns table(tbl text, col text)
language sql
security definer
stable
set search_path = public
as $func$
  select c.relname::text, a.attname::text
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    join pg_attribute a on a.attrelid = c.oid
   where n.nspname = 'public'
     and c.relkind in ('r','p')
     and a.attnum > 0
     and not a.attisdropped
     and a.atttypid in ('text'::regtype, 'varchar'::regtype)
     and (a.attname = 'path' or a.attname like '%\_path')
     and c.relname <> 'photo_path_extra'
  union
  -- joined through the catalogue rather than trusted: a registry row naming a
  -- table or column that is not there would otherwise break every sweep
  select e.tbl, e.col
    from public.photo_path_extra e
    join pg_class     c2 on c2.relname = e.tbl and c2.relkind in ('r','p')
    join pg_namespace n2 on n2.oid = c2.relnamespace and n2.nspname = 'public'
    join pg_attribute a2 on a2.attrelid = c2.oid and a2.attname = e.col
                        and a2.attnum > 0 and not a2.attisdropped;
$func$;


-- ---------- PART 3 of 7 : is anything still pointing at it ----------
-- One question, asked of one file, which is what the delete rule in PART 7
-- needs. Built from PART 2 each time rather than written out, so it cannot
-- fall behind the schema.
create or replace function public.photo_in_use(p text)
returns boolean
language plpgsql
security definer
stable
set search_path = public
as $func$
declare q text; hit boolean;
begin
  if p is null then return false; end if;
  select string_agg(format('select 1 from public.%I where %I = $1', c.tbl, c.col),
                    ' union all ')
    into q from public.photo_columns() c;
  -- no columns at all is a broken catalogue read, not a bucket of orphans:
  -- say everything is in use, so the sweep deletes nothing
  if q is null then return true; end if;
  execute 'select exists (' || q || ')' into hit using p;
  return coalesce(hit, false);
end $func$;


-- ---------- PART 4 of 7 : what is orphaned ----------
-- The same question asked of the whole bucket at once. An anti-join rather
-- than photo_in_use once per file: a bucket holding tens of thousands of pick
-- list thumbnails would otherwise be tens of thousands of round trips.
create or replace function public.orphan_photos()
returns table(path text, bytes bigint)
language plpgsql
security definer
stable
set search_path = public
as $func$
declare used text;
begin
  if not public.on_team(auth.uid()) then return; end if;   -- a stranger sweeps nothing

  select string_agg(format('select %I::text as p from public.%I where %I is not null',
                           c.col, c.tbl, c.col), ' union all ')
    into used from public.photo_columns() c;
  -- as above: nothing known means nothing swept, never everything swept
  if used is null then return; end if;

  return query execute format($q$
    with used as (%s)
    select o.name, coalesce((o.metadata->>'size')::bigint, 0)::bigint
      from storage.objects o
     where o.bucket_id = 'shipment-photos'
       and o.created_at < now() - public.photo_grace()
       and not exists (select 1 from used u where u.p = o.name)
  $q$, used);
end $func$;

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


-- ---------- PART 5 of 7 : twice a day, once between everybody ----------
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


-- ---------- PART 6 of 7 : who may ask ----------
revoke all on function public.photo_grace()                from public;
revoke all on function public.photo_columns()              from public;
revoke all on function public.photo_in_use(text)           from public;
revoke all on function public.orphan_photos()              from public;
revoke all on function public.orphan_photo_usage()         from public;
revoke all on function public.sweep_orphan_photos(boolean) from public;
grant execute on function public.photo_grace()                to authenticated;
grant execute on function public.photo_columns()              to authenticated;
grant execute on function public.photo_in_use(text)           to authenticated;
grant execute on function public.orphan_photos()              to authenticated;
grant execute on function public.orphan_photo_usage()         to authenticated;
grant execute on function public.sweep_orphan_photos(boolean) to authenticated;


-- ---------- PART 7 of 7 : being allowed to delete it ----------
-- The bucket's standing rule is "your own folder only", and an orphan is
-- usually somebody else's — left over from a box packed by whoever was on that
-- morning. This is the narrowest rule that lets the sweep work: it permits a
-- delete only of a file in this bucket, old enough to be past the grace
-- period, that no column found in PART 2 names. A file still in use is not
-- deletable through it however it is asked for, so the worst a mistaken sweep
-- can do is be refused.
-- Being allowed to delete it is not enough on its own. The storage API looks a
-- file up before it removes it, so a file the rules will not show is skipped
-- in silence — no error, nothing deleted, the count unchanged. That is why
-- there are two rules here and not one.
drop policy if exists "orphan_photos_team_select" on storage.objects;
create policy "orphan_photos_team_select" on storage.objects
  for select using (
    bucket_id = 'shipment-photos'
    and public.on_team(auth.uid())
    and created_at < now() - public.photo_grace()
    and not public.photo_in_use(name)
  );

drop policy if exists "orphan_photos_team_delete" on storage.objects;
create policy "orphan_photos_team_delete" on storage.objects
  for delete using (
    bucket_id = 'shipment-photos'
    and public.on_team(auth.uid())
    and created_at < now() - public.photo_grace()
    and not public.photo_in_use(name)
  );


-- ---------- what it is watching, if you ever want to look ----------
--   select * from public.photo_columns() order by tbl, col;
