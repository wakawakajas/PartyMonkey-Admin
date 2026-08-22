-- ============================================================
-- BUNDLE SKU — the day's bundling jobs
-- Run in the Supabase SQL Editor AFTER supabase-migration-ADHOC.sql
-- (which is where on_team comes from) and
-- supabase-migration-BUNDLE-SKU-TABLES.sql.
-- Safe to run more than once. Plain statements, no DO blocks.
--
-- One job is one combination to be packed today, and how many sets of it.
-- The pieces are copied in rather than pointed at: a job is a record of what
-- was packed, and a catalogue edited next week must not rewrite it.
-- ============================================================

create table if not exists public.bundle_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  combo_name text not null,
  image_url text,
  sets integer not null default 1,
  status text not null default 'pending',        -- 'pending' | 'completed'
  completed_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists public.bundle_job_items (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  job_id uuid not null references public.bundle_jobs(id) on delete cascade,
  sku text not null,
  qty integer not null default 1,                -- per set
  done boolean not null default false,
  sort_order integer not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists bundle_job_items_job_idx on public.bundle_job_items(job_id);
create index if not exists bundle_jobs_status_idx on public.bundle_jobs(status);

alter table public.bundle_jobs      enable row level security;
alter table public.bundle_job_items enable row level security;

drop policy if exists "team_select" on public.bundle_jobs;
drop policy if exists "team_insert" on public.bundle_jobs;
drop policy if exists "team_update" on public.bundle_jobs;
drop policy if exists "team_delete" on public.bundle_jobs;

create policy "team_select" on public.bundle_jobs
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.bundle_jobs
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.bundle_jobs
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.bundle_jobs
  for delete using (public.on_team(auth.uid()));

drop policy if exists "team_select" on public.bundle_job_items;
drop policy if exists "team_insert" on public.bundle_job_items;
drop policy if exists "team_update" on public.bundle_job_items;
drop policy if exists "team_delete" on public.bundle_job_items;

create policy "team_select" on public.bundle_job_items
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.bundle_job_items
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.bundle_job_items
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.bundle_job_items
  for delete using (public.on_team(auth.uid()));


-- A finished job is worth keeping for long enough to check what was packed,
-- and worthless after that. Nothing here is stored in a bucket -- the pictures
-- are BigSeller's own links -- so this deletes rows and hands back nothing.
create or replace function public.purge_old_bundle_jobs()
returns void
language plpgsql
security definer
set search_path = public
as $func$
begin
  -- an anonymous caller cleans up nothing
  if not public.on_team(auth.uid()) then return; end if;

  -- bundle_job_items goes with it on the cascade
  delete from public.bundle_jobs
   where status = 'completed'
     and completed_at is not null
     and completed_at < now() - interval '3 days';
end $func$;

revoke all on function public.purge_old_bundle_jobs() from public;
grant execute on function public.purge_old_bundle_jobs() to authenticated;
