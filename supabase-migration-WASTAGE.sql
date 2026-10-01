-- ============================================================
-- FINANCE — WASTAGE (and a POS ID on offline orders)
-- Run AFTER supabase-migration-OFFLINE-ORDERS.sql. Safe to run more than once.
--
-- Wasted sheets, priced from the Finance paper table. Was kept in each PC's
-- localStorage; now shared. The paper is kept by name and its cost worked out
-- when the line is added, so a later price change does not rewrite the past.
-- ============================================================

create table if not exists public.wastage (
  id uuid primary key default gen_random_uuid(),
  paper text,                 -- "Cavallo Plus (Gloss) 250gsm", as in Finance settings
  sheets numeric not null default 0,
  side text not null default 'single',   -- single | double | blank (paper only)
  cost numeric,
  reason text,
  wasted_on date not null default current_date,
  pos_id text,
  by_who text,
  created_at timestamptz not null default now(),
  created_by uuid references auth.users(id) default auth.uid()
);

alter table public.wastage enable row level security;
drop policy if exists "wastage_all" on public.wastage;
create policy "wastage_all" on public.wastage
  for all using (public.on_finance_team(auth.uid()) or public.is_admin(auth.uid()))
  with check (public.on_finance_team(auth.uid()) or public.is_admin(auth.uid()));

-- the reasons to pick from, shared; added to from the Wastage screen
alter table public.finance_settings add column if not exists wastage_reasons jsonb not null
  default '["Damage","Misalignment","Test / sample","Print defect","Paper jam","Wrong artwork"]'::jsonb;

alter table public.offline_orders add column if not exists pos_id text;
