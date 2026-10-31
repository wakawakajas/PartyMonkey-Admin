-- ============================================================
-- FINANCE — MONTH REPORT: "entered in POS" ticks
-- Run AFTER supabase-migration-WASTAGE.sql (uses on_finance_team).
-- Safe to run more than once.
--
-- One row per month that has been keyed into the shop's own POS. The totals
-- are kept as they were when it was ticked, so a line added or changed after
-- that shows the month as out of date instead of quietly changing it.
-- Untick = the row is deleted.
-- ============================================================

create table if not exists public.finance_months (
  month text primary key check (month ~ '^\d{4}-\d{2}$'),   -- "2026-10"
  pos_ref text,
  done_by text,
  done_at timestamptz not null default now(),
  sales numeric not null default 0,     -- as shown when ticked: sales +
  paper numeric not null default 0,     -- paper used, negative
  wastage numeric not null default 0,   -- wastage, negative
  updated_at timestamptz not null default now(),
  updated_by uuid references auth.users(id) default auth.uid()
);

alter table public.finance_months enable row level security;
drop policy if exists "finance_months_all" on public.finance_months;
create policy "finance_months_all" on public.finance_months
  for all using (public.on_finance_team(auth.uid()) or public.is_admin(auth.uid()))
  with check (public.on_finance_team(auth.uid()) or public.is_admin(auth.uid()));

-- signed-in users only, so no anon grant
grant select, insert, update, delete on public.finance_months to authenticated;
grant select, insert, update, delete on public.finance_months to service_role;

-- check:
--   select month, done_by, done_at, sales + paper + wastage as net from public.finance_months order by month desc;
