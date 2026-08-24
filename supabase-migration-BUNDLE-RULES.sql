-- ============================================================
-- BUNDLE SKU — the rules, out of the code and onto the screen
-- Run in the Supabase SQL Editor AFTER supabase-migration-BUNDLE-SHOP.sql.
-- Safe to run more than once. Plain statements, no DO blocks.
--
-- Four things were written into the app and had to be deployed to change:
-- which word in an order's file name means Plant Talks, which words Plant
-- Talks packs, which words mean a piece is made to order, and whether
-- PartyMonkey packs combinations only. They are one row here instead, so the
-- floor can change them on a phone between orders.
--
-- One row for the team, not one per person: this is how the shops work, not a
-- preference of whoever is holding the phone.
-- ============================================================

create table if not exists public.bundle_settings (
  id boolean primary key default true check (id),

  -- PT in an order's file name is Plant Talks; anything else is PartyMonkey
  pt_word text not null default 'PT',

  -- what a Plant Talks order packs, by name
  seed_words text not null default 'seed, seeds',

  -- a piece with one of these in its name is printed to order, and is tagged
  -- as such while it is packed
  made_to_order text not null default 'Custom',

  -- a PartyMonkey row that names a SKU and nothing else is picked off a shelf
  -- rather than packed out of pieces, so it is not made into a bundle
  pm_combos_only boolean not null default true,

  updated_at timestamptz not null default now()
);

insert into public.bundle_settings (id) values (true) on conflict (id) do nothing;

alter table public.bundle_settings enable row level security;

drop policy if exists "team_select" on public.bundle_settings;
drop policy if exists "team_insert" on public.bundle_settings;
drop policy if exists "team_update" on public.bundle_settings;

create policy "team_select" on public.bundle_settings
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.bundle_settings
  for insert with check (public.on_team(auth.uid()));
create policy "team_update" on public.bundle_settings
  for update using (public.on_team(auth.uid()));

-- Added after the first four: a hidden list of Bundle SKU's own. The pick
-- list's hidden list means somebody else prints this to order and it is left
-- off the sheet; here a piece named by this one is on the card but greyed --
-- not counted, not read out, and not waited for.
alter table public.bundle_settings
  add column if not exists hidden_words text not null default '';

-- ============================================================
-- Hidden words, as a list rather than a comma-separated field
-- One row per word, matched fuzzy -- anywhere in a piece's SKU name, not
-- bounded to a whole word -- so "cust" hides "Custom", "Customized" and
-- anything else carrying it. Add, search and edit as a list on Bundle SKU
-- Settings; this is Bundle SKU's own and the pick list keeps its own
-- separately.
-- ============================================================

create table if not exists public.bundle_hidden_words (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  term text not null,
  created_at timestamptz not null default now()
);

create unique index if not exists bundle_hidden_words_term_key
  on public.bundle_hidden_words (lower(term));

alter table public.bundle_hidden_words enable row level security;

drop policy if exists "team_select" on public.bundle_hidden_words;
drop policy if exists "team_insert" on public.bundle_hidden_words;
drop policy if exists "team_update" on public.bundle_hidden_words;
drop policy if exists "team_delete" on public.bundle_hidden_words;

create policy "team_select" on public.bundle_hidden_words
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.bundle_hidden_words
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.bundle_hidden_words
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.bundle_hidden_words
  for delete using (public.on_team(auth.uid()));
