-- Run this once in the Supabase SQL Editor (Project → SQL Editor → New query).
-- Creates the templates and shipments tables with row-level security so
-- each signed-in user can only ever see and modify their own data.

create table public.templates (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  data jsonb not null,
  sort_order integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, name)
);

alter table public.templates enable row level security;

create policy "Users can view own templates"
  on public.templates for select
  using (auth.uid() = user_id);

create policy "Users can insert own templates"
  on public.templates for insert
  with check (auth.uid() = user_id);

create policy "Users can update own templates"
  on public.templates for update
  using (auth.uid() = user_id);

create policy "Users can delete own templates"
  on public.templates for delete
  using (auth.uid() = user_id);

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
