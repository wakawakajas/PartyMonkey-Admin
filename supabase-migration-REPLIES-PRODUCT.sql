-- ============================================================
-- A REPLY CAN SEND A PRODUCT
-- Run in the Supabase SQL Editor AFTER supabase-migration-REPLIES-SHELF.sql.
-- Safe to run more than once.
--
-- "Which spray is for caterpillars?" is answered properly by the listing
-- itself — the picture, the price, the variations, the link that opens the
-- item. DuoKe has a Product tab that sends exactly that card; this is how a
-- reply written in Pigu gets to use it.
--
-- Pigu never sees the shop's catalogue and does not need to. What travels is
-- the product's NAME, and on the shop PC Macro Studio does what a person
-- does: opens the Product tab, searches that name, presses Send on the row.
-- A name matching nothing sends nothing and says so, because a wrong product
-- card reads to a buyer as an answer.
--
--   reply_products              the names worth keeping — the twenty listings
--                               that get asked about. Typed once, tapped
--                               after that.
--   reply_messages.products     what this reply is sending, decided before
--                               Enter: [{"query": "...", "label": "..."}]
-- ============================================================

-- ---------- PART 1 of 3 : the shelf of listings ----------
create table if not exists public.reply_products (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  -- what to type into DuoKe's product search
  query text not null,
  -- what to call it on the screen. Empty means show the query itself.
  label text not null default '',
  -- how often it has actually been sent, so the ones that get asked about
  -- rise to the front of the strip
  uses integer not null default 0,
  used_at timestamptz,
  created_at timestamptz not null default now()
);

create unique index if not exists reply_products_query_key
  on public.reply_products (lower(btrim(query)));

create index if not exists reply_products_rank_idx
  on public.reply_products (uses desc, created_at desc);

alter table public.reply_products enable row level security;

drop policy if exists "team_select" on public.reply_products;
drop policy if exists "team_insert" on public.reply_products;
drop policy if exists "team_update" on public.reply_products;
drop policy if exists "team_delete" on public.reply_products;

create policy "team_select" on public.reply_products
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.reply_products
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.reply_products
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.reply_products
  for delete using (public.on_team(auth.uid()));


-- ---------- PART 2 of 3 : what this reply is sending ----------
alter table public.reply_messages
  add column if not exists products jsonb not null default '[]'::jsonb;


-- ---------- PART 3 of 3 : check ----------
--   select query, uses from public.reply_products order by uses desc;
--   select buyer, products from public.reply_messages
--    where jsonb_array_length(products) > 0 order by answered_at desc limit 5;
