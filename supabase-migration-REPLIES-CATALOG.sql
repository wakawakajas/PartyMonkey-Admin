-- ============================================================
-- THE SHOP'S OWN LISTINGS, SYNCED
-- Run in the Supabase SQL Editor AFTER supabase-migration-REPLIES-PRODUCT.sql.
-- Safe to run more than once.
--
-- Typing a listing's name to keep it as a chip worked, and it is the wrong way
-- round: the shop already has a catalogue, and DuoKe's Product tab is showing
-- it. So Macro Studio reads that list and writes it here, and the Replies
-- screen searches it — type "caterpillar", see the spray, tap it, send it.
--
-- WHAT IS STORED IS WHAT THE PANEL SHOWS: the title, the price as written, the
-- SKU and the stock. Not an id: the send is still "search this name and press
-- Send on the row that matches", because that is the only handle DuoKe offers
-- from outside. The title is therefore the key, and a renamed listing arrives
-- as a new row while the old one stops being refreshed.
--
-- seen_at is what makes that visible. A row nobody has seen for a week is a
-- listing that has been renamed, delisted or sold out of the panel, and the
-- screen can grey it out rather than offer it as though it were still there.
-- ============================================================

-- ---------- PART 1 of 3 : the catalogue ----------
create table if not exists public.reply_catalog (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  -- which shop's panel it was read from, where the PC answers for more than
  -- one. Empty when the panel does not say.
  shop text not null default '',
  title text not null,
  -- exactly as the panel writes them, because a price is a range ("0.59 ~
  -- 2.00 SGD") as often as it is a number
  price text not null default '',
  sku text not null default '',
  stock text not null default '',
  -- how often a reply has sent this one, so the search can put what actually
  -- gets asked about at the top
  uses integer not null default 0,
  used_at timestamptz,
  first_seen_at timestamptz not null default now(),
  seen_at timestamptz not null default now()
);

-- The title is the handle, so it is the key. Two shops selling a listing of
-- the same name are two rows, because the send has to happen in the right
-- shop's panel.
--
-- Case-folded, and that is deliberate: DuoKe writes a title one way and this
-- is not the place to decide two spellings are two products. Note that an
-- index on an EXPRESSION is not something PostgREST's on_conflict can resolve
-- -- it matches constraints on columns -- so the sync reads what is there and
-- splits its write into inserts and updates rather than upserting. Nothing
-- about this table has to change for that.
create unique index if not exists reply_catalog_title_key
  on public.reply_catalog (shop, lower(btrim(title)));

create index if not exists reply_catalog_recent_idx
  on public.reply_catalog (uses desc, seen_at desc);

alter table public.reply_catalog enable row level security;

drop policy if exists "team_select" on public.reply_catalog;
drop policy if exists "team_insert" on public.reply_catalog;
drop policy if exists "team_update" on public.reply_catalog;
drop policy if exists "team_delete" on public.reply_catalog;

create policy "team_select" on public.reply_catalog
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.reply_catalog
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.reply_catalog
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.reply_catalog
  for delete using (public.on_team(auth.uid()));


-- ---------- PART 2 of 3 : asking for a fresh read ----------
-- Same shape as the chat history: a stamp on a row, because the only thing
-- that can read the catalogue is the PC with DuoKe on it and it is on its own
-- clock. catalog_at is when it last managed one, so a second press re-reads
-- rather than being answered by what is already there.
alter table public.reply_sync
  add column if not exists catalog_wanted_at timestamptz;
alter table public.reply_sync
  add column if not exists catalog_at timestamptz;
alter table public.reply_sync
  add column if not exists catalog_count integer not null default 0;


-- ---------- PART 3 of 3 : check ----------
--   select count(*), max(seen_at) from public.reply_catalog;
--   select title, price, sku from public.reply_catalog
--    order by uses desc, seen_at desc limit 10;
--   select catalog_at, catalog_count from public.reply_sync;
