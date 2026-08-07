-- Run this once in the Supabase SQL Editor (Project → SQL Editor → New query).
-- Creates the templates table with row-level security so each signed-in
-- user can only ever see and modify their own saved templates.

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
