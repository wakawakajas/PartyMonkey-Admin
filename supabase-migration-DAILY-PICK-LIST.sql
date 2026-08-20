-- ============================================================
-- DAILY PICK LIST
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql and
-- supabase-migration-ADHOC.sql (which is where public.on_team comes from).
-- Safe to run more than once, and safe to run as one batch — the Editor
-- wraps a pasted file in a transaction, so nothing here may raise on a
-- second run or it would undo everything above it.
--
-- A pick list is a PDF somebody uploads in the morning. It becomes rows on
-- a shared screen that several people pick from at once, and once it is
-- finished it is worth keeping for a few days and worthless after that.
-- ============================================================

-- ---------- PART 1 of 6 : who can open it ----------
-- defaults to false, so nobody sees it without being given it
alter table public.profiles
  add column if not exists can_daily_pick_list boolean not null default false;

-- Who can open it — grant it from Users & access, or here:
--   update public.profiles set can_daily_pick_list = true where email = 'someone@example.com';


-- ---------- PART 2 of 6 : the tables ----------
-- The list itself. title is the uploaded PDF's own file name, kept exactly
-- as it arrived — that is what the floor calls it, so renaming it here would
-- only mean two names for one morning's work.
create table if not exists public.pick_lists (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text not null,
  -- pending -> completed, and nothing comes back: a completed list is locked
  -- and on a five day timer
  status text not null default 'pending'
    check (status in ('pending','completed')),
  completed_at timestamptz,
  completed_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

-- One line off the PDF. status is null until somebody touches it, which is
-- what "unhandled" means everywhere in the app — a column of its own for it
-- would only be a second way of saying the same thing.
create table if not exists public.pick_list_items (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  list_id uuid not null references public.pick_lists(id) on delete cascade,
  sort_order integer not null default 0,
  sku text not null default '',
  shelf text not null default '',
  qty integer not null default 1,
  -- where the row's thumbnail sits in the bucket; null if the PDF had none
  image_path text,
  status text check (status in ('ok','adhoc','request')),
  -- required before a line may be marked ad-hoc; free text otherwise
  remarks text not null default '',
  -- who last touched it, so a list being picked by three people at once can
  -- say which of them did what
  acted_by uuid references auth.users(id) on delete set null,
  acted_at timestamptz,
  created_at timestamptz not null default now()
);

-- the two questions these tables are ever asked: what is on the dashboard,
-- and what is on this list
create index if not exists pick_lists_status_idx on public.pick_lists(status, created_at desc);
create index if not exists pick_list_items_list_idx on public.pick_list_items(list_id, sort_order);
-- and the one the five-day sweep asks
create index if not exists pick_lists_completed_idx on public.pick_lists(completed_at)
  where completed_at is not null;

alter table public.pick_lists      enable row level security;
alter table public.pick_list_items enable row level security;


-- ---------- PART 3 of 6 : shared with the team ----------
-- These are shop floor lists, the same decision as Ad-Hoc Picking: everyone
-- with an account works the same list, and the interface gate in PART 1 is
-- what decides who is shown the tile.
drop policy if exists "team_select" on public.pick_lists;
drop policy if exists "team_insert" on public.pick_lists;
drop policy if exists "team_update" on public.pick_lists;
drop policy if exists "team_delete" on public.pick_lists;

create policy "team_select" on public.pick_lists
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.pick_lists
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.pick_lists
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.pick_lists
  for delete using (public.on_team(auth.uid()));

drop policy if exists "team_select" on public.pick_list_items;
drop policy if exists "team_insert" on public.pick_list_items;
drop policy if exists "team_update" on public.pick_list_items;
drop policy if exists "team_delete" on public.pick_list_items;

create policy "team_select" on public.pick_list_items
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.pick_list_items
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
-- A completed list is locked. Doing it here rather than in the app means it
-- also holds for a row edited from somewhere else, and it is the only place
-- the lock can actually be enforced.
create policy "team_update" on public.pick_list_items
  for update using (
    public.on_team(auth.uid())
    and exists (select 1 from public.pick_lists l
                 where l.id = list_id and l.status = 'pending'));
create policy "team_delete" on public.pick_list_items
  for delete using (public.on_team(auth.uid()));


-- ---------- PART 4 of 6 : the row thumbnails ----------
-- They live at <uid>/picklist/<name>.jpg in the bucket everything else uses.
-- That bucket's standing rule is "your own folder only", which would hide the
-- pictures from everyone except whoever uploaded the PDF — so, as with SKU
-- photos, this adds a narrower second permission for this one folder.
drop policy if exists "pick_list_photos_team_select" on storage.objects;
drop policy if exists "pick_list_photos_team_delete" on storage.objects;

create policy "pick_list_photos_team_select" on storage.objects
  for select using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[2] = 'picklist'
    and public.on_team(auth.uid())
  );

-- wider than "your own", on purpose: whoever opens the app first is the one
-- who runs the five-day sweep, and the files it clears are somebody else's
create policy "pick_list_photos_team_delete" on storage.objects
  for delete using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[2] = 'picklist'
    and public.on_team(auth.uid())
  );


-- ---------- PART 5 of 6 : live updates between devices ----------
-- Several people pick one list at once, so a tick on one phone has to show up
-- on the others without anybody refreshing.
alter table public.pick_lists      replica identity full;
alter table public.pick_list_items replica identity full;

-- Asked first rather than attempted and forgiven. Adding a table that is
-- already published raises 42710, and the SQL Editor runs this file as one
-- transaction — so that error would roll back everything above it.
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'pick_lists')
  then
    alter publication supabase_realtime add table public.pick_lists;
  end if;
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'pick_list_items')
  then
    alter publication supabase_realtime add table public.pick_list_items;
  end if;
end $$;


-- ---------- PART 6 of 6 : clearing up after five days ----------
-- A completed list is worth keeping for long enough to check what was picked,
-- and worthless after that. Thumbnails are the bulk of it: a morning's list is
-- a hundred small pictures, and left alone they would fill the bucket.
--
-- It hands back the storage paths it removed rather than only deleting rows,
-- because deleting a row in pick_list_items does not remove the file itself.
-- The app takes that list straight to the storage API. Reading the paths
-- before the delete is why this is plpgsql and not one statement.
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
     and l.completed_at < now() - interval '5 days';

  if doomed is null then return; end if;

  return query
    select i.image_path from public.pick_list_items i
     where i.list_id = any(doomed) and i.image_path is not null;

  -- pick_list_items goes with it on the cascade
  delete from public.pick_lists where id = any(doomed);
end $func$;

revoke all on function public.purge_old_pick_lists() from public;
grant execute on function public.purge_old_pick_lists() to authenticated;
