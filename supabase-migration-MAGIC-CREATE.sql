-- ============================================================
-- MAGIC CREATE
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql and
-- supabase-migration-ADHOC.sql (which is where public.on_team comes from).
-- Safe to run more than once, and safe to run as one batch — the Editor
-- wraps a pasted file in a transaction, so nothing here may raise on a
-- second run or it would undo everything above it.
--
-- A batch is the morning's order spreadsheet. It becomes rows on a shared
-- screen: one group to an order, one line to a SKU. Most lines are read and
-- marked done and that is the whole of it. A few open a print tile, and
-- those are the ones that come out the other end as PDFs.
--
-- It sits inside Print template, so it is gated by can_print — the same key
-- that opens Custom Template and Gift Tag. There is no separate permission:
-- a tile behind a door already has the door.
-- ============================================================

-- ---------- PART 1 of 5 : the tables ----------
-- The batch. title is the uploaded file's own name, kept exactly as it
-- arrived — it carries the timestamp the export was taken at, which is a
-- better label than anything we would invent.
create table if not exists public.magic_batches (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text not null,
  -- every column the sheet had, and which of them we read as what. Kept so a
  -- mapping mistake on Monday is fixable on Tuesday without re-uploading.
  source_columns jsonb not null default '[]'::jsonb,
  mapping jsonb not null default '{}'::jsonb,
  -- open -> completed, and completion starts a three day clock
  status text not null default 'open'
    check (status in ('open','completed')),
  completed_at timestamptz,
  completed_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

-- One line off the sheet. status is null until somebody touches it, which is
-- what "not done" means everywhere in the app — a column of its own for it
-- would only be a second way of saying the same thing.
--
-- tile is the operator's own choice and nothing infers it: null means this
-- line never opens a builder at all, which today is most of them.
create table if not exists public.magic_items (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  batch_id uuid not null references public.magic_batches(id) on delete cascade,
  sort_order integer not null default 0,
  -- not unique: one order arrives as several lines, one per SKU
  order_id text not null default '',
  buyer text not null default '',
  sku text not null default '',
  -- the Merchant SKU up to its first hyphen: "Custom TY Card (A6 Fold)"
  product text not null default '',
  -- and the rest of it: "Rubber Ducky"
  design_name text not null default '',
  qty integer not null default 1,
  -- what the buyer asked to have printed on it
  message text not null default '',
  note text not null default '',
  -- what the customer sees on the shop listing. Shown beside the line so the
  -- operator knows which design is meant; never printed, never fetched.
  image_url text not null default '',
  raw jsonb not null default '{}'::jsonb,
  status text check (status in ('done')),
  -- which print tile builds it; null is a line finished by Done alone
  tile text,
  -- a per-order tile's builder snapshot, in the shape templates already store
  design jsonb,
  -- a shared-sheet tile's contribution: this line's place on the sheet the
  -- batch is building. Held per line rather than as one sheet row, because
  -- two people adding to one sheet would otherwise overwrite each other.
  sheet_item jsonb,
  -- bucket paths for artwork uploaded into the builder, front and back
  art jsonb not null default '{}'::jsonb,
  exported_at timestamptz,
  -- who last touched it, so a batch worked by three people at once can say
  -- which of them did what
  acted_by uuid references auth.users(id) on delete set null,
  acted_at timestamptz,
  created_at timestamptz not null default now()
);

-- the three questions these tables are ever asked: what is on the overview,
-- what is on this batch, and which lines belong to this order
create index if not exists magic_batches_status_idx
  on public.magic_batches(status, created_at desc);
create index if not exists magic_items_batch_idx
  on public.magic_items(batch_id, sort_order);
create index if not exists magic_items_order_idx
  on public.magic_items(batch_id, order_id);
-- and the one the three-day sweep asks
create index if not exists magic_batches_completed_idx
  on public.magic_batches(completed_at) where completed_at is not null;

alter table public.magic_batches enable row level security;
alter table public.magic_items   enable row level security;


-- ---------- PART 2 of 5 : shared with the team ----------
-- The same decision as the pick list: everyone with an account works the
-- same batch, and can_print is what decides who is shown the tile.
drop policy if exists "team_select" on public.magic_batches;
drop policy if exists "team_insert" on public.magic_batches;
drop policy if exists "team_update" on public.magic_batches;
drop policy if exists "team_delete" on public.magic_batches;

create policy "team_select" on public.magic_batches
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.magic_batches
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.magic_batches
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.magic_batches
  for delete using (public.on_team(auth.uid()));

drop policy if exists "team_select" on public.magic_items;
drop policy if exists "team_insert" on public.magic_items;
drop policy if exists "team_update" on public.magic_items;
drop policy if exists "team_delete" on public.magic_items;

create policy "team_select" on public.magic_items
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.magic_items
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
-- A completed batch is read only. Doing it here rather than in the app means
-- it also holds for a row edited from somewhere else, and it is the only
-- place the lock can actually be enforced.
create policy "team_update" on public.magic_items
  for update using (
    public.on_team(auth.uid())
    and exists (select 1 from public.magic_batches b
                 where b.id = batch_id and b.status = 'open'));
create policy "team_delete" on public.magic_items
  for delete using (public.on_team(auth.uid()));


-- ---------- PART 3 of 5 : the artwork ----------
-- Uploaded artwork lives at <uid>/magic/<name>.png in the bucket everything
-- else uses. That bucket's standing rule is "your own folder only", which
-- would hide a colleague's artwork from the person exporting the batch — so,
-- as with the pick list thumbnails, this adds a narrower second permission
-- for this one folder.
drop policy if exists "magic_art_team_select" on storage.objects;
drop policy if exists "magic_art_team_delete" on storage.objects;

create policy "magic_art_team_select" on storage.objects
  for select using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[2] = 'magic'
    and public.on_team(auth.uid())
  );

-- wider than "your own", on purpose: whoever opens the screen first runs the
-- three-day sweep, and the files it clears are somebody else's
create policy "magic_art_team_delete" on storage.objects
  for delete using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[2] = 'magic'
    and public.on_team(auth.uid())
  );


-- ---------- PART 4 of 5 : live updates between devices ----------
-- Several people work one batch at once, so a line marked done on one screen
-- has to show up on the others without anybody refreshing.
alter table public.magic_batches replica identity full;
alter table public.magic_items   replica identity full;

-- Asked first rather than attempted and forgiven. Adding a table that is
-- already published raises 42710, and the SQL Editor runs this file as one
-- transaction — so that error would roll back everything above it.
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'magic_batches')
  then
    alter publication supabase_realtime add table public.magic_batches;
  end if;
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'magic_items')
  then
    alter publication supabase_realtime add table public.magic_items;
  end if;
end $$;


-- ---------- PART 5 of 5 : clearing up after three days ----------
-- A completed batch is worth keeping long enough to check what was produced,
-- and worthless after that. Artwork is the bulk of it: a day's batch is a
-- few hundred print-resolution files, and left alone they would fill the
-- bucket.
--
-- It hands back the storage paths it removed rather than only deleting rows,
-- because deleting a row in magic_items does not remove the file itself. The
-- app takes that list straight to the storage API. Reading the paths before
-- the delete is why this is plpgsql and not one statement.
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

  -- art is { "front": "<path>", "back": "<path>" }, either side possibly
  -- absent, so the values are unrolled rather than named
  return query
    select v.value from public.magic_items i, jsonb_each_text(i.art) as v
     where i.batch_id = any(doomed) and v.value <> '';

  -- magic_items goes with it on the cascade
  delete from public.magic_batches where id = any(doomed);
end $func$;

revoke all on function public.purge_old_magic_batches() from public;
grant execute on function public.purge_old_magic_batches() to authenticated;
