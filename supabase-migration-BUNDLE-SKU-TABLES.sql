-- ============================================================
-- BUNDLE SKU — the combination catalogue
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql
-- and supabase-migration-ADHOC.sql (which is where on_team comes from).
-- Safe to run more than once. Plain statements, no DO blocks.
--
-- A combination is a shop floor fact, not one person's note, so these are
-- shared by everyone with an account — the same decision as the warehouse
-- catalogue.
-- ============================================================

-- the combination itself: one row per combination SKU name
create table if not exists public.bundle_skus (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null unique,
  image_url text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- what is inside it: one row per sub-SKU, in the order the sheet listed them
create table if not exists public.bundle_sku_items (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  bundle_id uuid not null references public.bundle_skus(id) on delete cascade,
  sku text not null,
  qty integer not null default 1,
  sort_order integer not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists bundle_sku_items_bundle_idx on public.bundle_sku_items(bundle_id);
create index if not exists bundle_skus_name_idx on public.bundle_skus(name);

alter table public.bundle_skus      enable row level security;
alter table public.bundle_sku_items enable row level security;

drop policy if exists "team_select" on public.bundle_skus;
drop policy if exists "team_insert" on public.bundle_skus;
drop policy if exists "team_update" on public.bundle_skus;
drop policy if exists "team_delete" on public.bundle_skus;

create policy "team_select" on public.bundle_skus
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.bundle_skus
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.bundle_skus
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.bundle_skus
  for delete using (public.on_team(auth.uid()));

drop policy if exists "team_select" on public.bundle_sku_items;
drop policy if exists "team_insert" on public.bundle_sku_items;
drop policy if exists "team_update" on public.bundle_sku_items;
drop policy if exists "team_delete" on public.bundle_sku_items;

create policy "team_select" on public.bundle_sku_items
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.bundle_sku_items
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.bundle_sku_items
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.bundle_sku_items
  for delete using (public.on_team(auth.uid()));
