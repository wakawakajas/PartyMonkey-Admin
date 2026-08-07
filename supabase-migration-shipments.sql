-- One-time migration to add the Shipment feature (order IDs + descriptions).
-- Run this once in the Supabase SQL Editor (Project → SQL Editor → New
-- query) — make sure the box is empty before pasting, then Run.
-- Safe to run once; running it a second time will error on the CREATE
-- TABLE line since the table would already exist (that's expected, not
-- a problem — it just means you already ran it).

create table public.shipments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  order_id text not null,
  description text,
  created_at timestamptz not null default now()
);

alter table public.shipments enable row level security;

create policy "Users can view own shipments"
  on public.shipments for select
  using (auth.uid() = user_id);

create policy "Users can insert own shipments"
  on public.shipments for insert
  with check (auth.uid() = user_id);

create policy "Users can update own shipments"
  on public.shipments for update
  using (auth.uid() = user_id);

create policy "Users can delete own shipments"
  on public.shipments for delete
  using (auth.uid() = user_id);
