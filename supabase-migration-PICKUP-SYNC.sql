-- ============================================================
-- STORE PICK UP — "From BigSeller" button
-- Run AFTER supabase-migration-SHARED-PICKUPS.sql (on_pickup_team comes from
-- there). Safe to run more than once. Plain statements, no DO blocks.
--
-- One row is one press of the button. Pigu cannot reach BigSeller or DuoKe
-- itself -- it is a web page -- so the row is the hand-off to Macro Studio on
-- the shop PC, which has both open:
--
--   queued    Pigu: the button was pressed
--   reading   Macro Studio: reading BigSeller New orders and DuoKe chats
--   found     Macro Studio: `found` holds the pick up orders it saw
--   shipping  Pigu: added them to Store Pick Up, `ship_ids` says which to ship
--   sending   Macro Studio: pressing Ship on them in BigSeller
--   done      Macro Studio: `result` says what happened to each
--   failed    either side: `error` says why
--
-- Pigu adds the pick ups itself, as the person who pressed the button, so the
-- rows are theirs and the usual "added" notification goes out from them.
-- ============================================================

create table if not exists public.pickup_sync_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  status text not null default 'queued',
  found jsonb not null default '[]'::jsonb,
  ship_ids jsonb not null default '[]'::jsonb,
  result jsonb not null default '[]'::jsonb,
  error text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists pickup_sync_jobs_status_idx on public.pickup_sync_jobs(status);

alter table public.pickup_sync_jobs enable row level security;

drop policy if exists "team_select" on public.pickup_sync_jobs;
drop policy if exists "team_insert" on public.pickup_sync_jobs;
drop policy if exists "team_update" on public.pickup_sync_jobs;
drop policy if exists "team_delete" on public.pickup_sync_jobs;

-- the pick up team, and the Macro Studio sync account (on_team), which is the
-- one that reads BigSeller and ships
create policy "team_select" on public.pickup_sync_jobs
  for select using (public.on_pickup_team(auth.uid()) or public.on_team(auth.uid()));
create policy "team_insert" on public.pickup_sync_jobs
  for insert with check (public.on_pickup_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.pickup_sync_jobs
  for update using (public.on_pickup_team(auth.uid()) or public.on_team(auth.uid()));
create policy "team_delete" on public.pickup_sync_jobs
  for delete using (public.on_pickup_team(auth.uid()));
