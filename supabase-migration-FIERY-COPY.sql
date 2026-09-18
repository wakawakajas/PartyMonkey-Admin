-- ============================================================
-- FIERY — copied files printed order by order, each SKU on its own preset
-- Run in the Supabase SQL Editor AFTER supabase-migration-FIERY.sql.
-- Safe to run more than once.
--
-- Copy File puts a SKU's artwork into Pigu Today PRINT. Printing it was then
-- the same trip into Command WorkStation as everything else, once per file,
-- with the preset remembered by whoever was at the press. Now an order's
-- copied files are sent from the batch: each SKU carries the preset it prints
-- on, and a batch's worth goes to the Fiery in the order the batch lists them,
-- each line with its own quantity as the copies.
-- ============================================================

-- ---------- PART 1 of 2 : the order they go in ----------
-- run_id  one press of Print; every job it queued shares it
-- seq     where in that press this job comes. Rows inserted together share
--         one created_at, so this is what keeps them in order — and a file
--         still on its way through the NAS holds back the rest of its run
--         rather than being overtaken by them.
alter table public.fiery_jobs add column if not exists run_id text not null default '';
alter table public.fiery_jobs add column if not exists seq integer not null default 0;

drop index if exists fiery_jobs_queued_idx;
create index if not exists fiery_jobs_queued_idx
  on public.fiery_jobs(status, created_at, seq);


-- ---------- PART 2 of 2 : the preset each SKU prints on ----------
-- Set from the Print pop-up and shared: the next person to print that SKU,
-- on any machine, is offered the same preset without being asked.
create table if not exists public.fiery_sku_presets (
  sku text primary key,
  preset_id text not null,
  preset_name text not null default '',
  updated_by uuid references auth.users(id) on delete set null,
  updated_at timestamptz not null default now()
);

alter table public.fiery_sku_presets enable row level security;

drop policy if exists "team_select" on public.fiery_sku_presets;
drop policy if exists "team_insert" on public.fiery_sku_presets;
drop policy if exists "team_update" on public.fiery_sku_presets;
drop policy if exists "team_delete" on public.fiery_sku_presets;
create policy "team_select" on public.fiery_sku_presets
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.fiery_sku_presets
  for insert with check (public.on_team(auth.uid()));
create policy "team_update" on public.fiery_sku_presets
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.fiery_sku_presets
  for delete using (public.on_team(auth.uid()));

-- ---------- check ----------
--   select sku, preset_name, updated_at from public.fiery_sku_presets order by sku;
