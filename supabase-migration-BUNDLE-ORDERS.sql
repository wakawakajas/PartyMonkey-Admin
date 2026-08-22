-- ============================================================
-- BUNDLE SKU — the day's order, above its bundles
-- Run in the Supabase SQL Editor AFTER supabase-migration-BUNDLE-JOBS.sql.
-- Safe to run more than once. Plain statements, no DO blocks.
--
-- A file read in the morning is an order: it has a name, a time, and the
-- bundles that came off it. The jobs hang under it so that finishing the
-- order finishes them, and deleting it deletes them.
-- ============================================================

create table if not exists public.bundle_orders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text not null,
  status text not null default 'pending',        -- 'pending' | 'completed'
  completed_at timestamptz,
  created_at timestamptz not null default now()
);

alter table public.bundle_jobs
  add column if not exists order_id uuid references public.bundle_orders(id) on delete cascade;

create index if not exists bundle_jobs_order_idx on public.bundle_jobs(order_id);
create index if not exists bundle_orders_status_idx on public.bundle_orders(status);

alter table public.bundle_orders enable row level security;

drop policy if exists "team_select" on public.bundle_orders;
drop policy if exists "team_insert" on public.bundle_orders;
drop policy if exists "team_update" on public.bundle_orders;
drop policy if exists "team_delete" on public.bundle_orders;

create policy "team_select" on public.bundle_orders
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.bundle_orders
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.bundle_orders
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.bundle_orders
  for delete using (public.on_team(auth.uid()));


-- Three days after an order is finished it is worth nothing, and its bundles
-- go with it on the cascade. Jobs from before this migration have no order
-- above them, so they are swept on their own completion date as before.
create or replace function public.purge_old_bundle_jobs()
returns void
language plpgsql
security definer
set search_path = public
as $func$
begin
  -- an anonymous caller cleans up nothing
  if not public.on_team(auth.uid()) then return; end if;

  delete from public.bundle_orders
   where status = 'completed'
     and completed_at is not null
     and completed_at < now() - interval '3 days';

  delete from public.bundle_jobs
   where order_id is null
     and status = 'completed'
     and completed_at is not null
     and completed_at < now() - interval '3 days';
end $func$;

revoke all on function public.purge_old_bundle_jobs() from public;
grant execute on function public.purge_old_bundle_jobs() to authenticated;


-- When the work actually began, as against when the file was read: an order
-- uploaded at eight and started at ten took two hours, and the pick list card
-- has always said so. Null until somebody starts.
alter table public.bundle_orders add column if not exists started_at timestamptz;
alter table public.bundle_jobs   add column if not exists started_at timestamptz;
