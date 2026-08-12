-- ============================================================
-- AD-HOC PICKING + WAREHOUSE SKUs
-- Run in the Supabase SQL Editor, AFTER supabase-migration-ACCESS.sql.
-- Safe to run more than once. Plain statements, no DO blocks.
--
-- These are shop floor lists, so they are shared by everyone you have given
-- an account — the same decision as Store Pick Up. Orders, shipping and
-- print templates stay private per account.
-- ============================================================

-- ---------- PART 1 of 4 : who is on the team ----------
create or replace function public.on_team(uid uuid)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select exists (select 1 from public.profiles where user_id = uid);
$func$;


-- ---------- PART 2 of 4 : the tables ----------
-- the warehouse catalogue: every SKU you stock, imported from a spreadsheet
create table if not exists public.warehouse_skus (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  sku text not null unique,
  created_at timestamptz not null default now()
);

create table if not exists public.adhoc_orders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  order_id text not null,
  sort_order integer not null default 0,
  created_at timestamptz not null default now()
);

create table if not exists public.adhoc_items (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  order_ref uuid not null references public.adhoc_orders(id) on delete cascade,
  sku text not null,
  qty integer not null default 1,
  picked boolean not null default false,
  picked_at timestamptz,
  sort_order integer not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists adhoc_items_order_idx on public.adhoc_items(order_ref);
create index if not exists warehouse_skus_sku_idx on public.warehouse_skus(sku);

alter table public.warehouse_skus enable row level security;
alter table public.adhoc_orders   enable row level security;
alter table public.adhoc_items    enable row level security;


-- ---------- PART 3 of 4 : shared with the team ----------
drop policy if exists "team_select" on public.warehouse_skus;
drop policy if exists "team_insert" on public.warehouse_skus;
drop policy if exists "team_update" on public.warehouse_skus;
drop policy if exists "team_delete" on public.warehouse_skus;

create policy "team_select" on public.warehouse_skus
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.warehouse_skus
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.warehouse_skus
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.warehouse_skus
  for delete using (public.on_team(auth.uid()));

drop policy if exists "team_select" on public.adhoc_orders;
drop policy if exists "team_insert" on public.adhoc_orders;
drop policy if exists "team_update" on public.adhoc_orders;
drop policy if exists "team_delete" on public.adhoc_orders;

create policy "team_select" on public.adhoc_orders
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.adhoc_orders
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.adhoc_orders
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.adhoc_orders
  for delete using (public.on_team(auth.uid()));

drop policy if exists "team_select" on public.adhoc_items;
drop policy if exists "team_insert" on public.adhoc_items;
drop policy if exists "team_update" on public.adhoc_items;
drop policy if exists "team_delete" on public.adhoc_items;

create policy "team_select" on public.adhoc_items
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.adhoc_items
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.adhoc_items
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.adhoc_items
  for delete using (public.on_team(auth.uid()));


-- ---------- PART 4 of 4 : live updates between devices ----------
alter table public.adhoc_orders replica identity full;
alter table public.adhoc_items  replica identity full;
