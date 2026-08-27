-- ============================================================
-- A DESIGN'S OWN BLEED, SHARED WITH THE PRINT TEAM
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql and
-- supabase-migration-SHARED-TEMPLATES.sql (which is where
-- public.on_print_team comes from).
-- Safe to run more than once, and safe to run as one batch — the Editor
-- wraps a pasted file in a transaction, so nothing here may raise on a
-- second run or it would undo everything above it.
--
-- A sheet has one bleed and most designs follow it. The few that must not —
-- artwork drawn right to the edge, a card that keeps cutting short — keep
-- their own figure instead. That figure belongs to the design, not to the
-- person who worked it out: whoever prints that card next should print it the
-- way it was proved, on their machine, without asking. Kept here rather than
-- in a browser for exactly that reason.
-- ============================================================

-- ---------- PART 1 of 3 : the list ----------
-- One row per design file, keyed by its path in the designs folder. The path
-- IS the identity: the same file laid on a sheet next month is the same
-- design, whatever the line on that sheet happens to be called.
create table if not exists public.design_bleeds (
  -- lower case, always: "Cards/AAA.png" and "cards/aaa.png" are one design and
  -- must not become two rows that disagree about how far it bleeds. The app
  -- lowers it before writing; the check is here so a future caller that
  -- forgets is told rather than quietly splitting the design in two.
  path text primary key check (path = lower(path)),
  -- millimetres, the same unit the sheet's own bleed is held in. A row exists
  -- only while the design keeps its own bleed — handing it back to the sheet
  -- deletes the row rather than writing a zero, because zero bleed and "follow
  -- the sheet" are different instructions.
  mm numeric not null check (mm >= 0),
  -- who set it last. Not ownership: anyone on the print team may correct it,
  -- the same as the templates themselves.
  user_id uuid not null references auth.users(id) on delete cascade,
  updated_at timestamptz not null default now()
);

alter table public.design_bleeds enable row level security;


-- ---------- PART 2 of 3 : shared with the print team ----------
-- Deliberately wider than "your own". A bleed only its author can change is a
-- bleed that stays wrong on everybody else's screen, which is the whole
-- problem this table exists to end.
drop policy if exists "print_team_select" on public.design_bleeds;
drop policy if exists "print_team_insert" on public.design_bleeds;
drop policy if exists "print_team_update" on public.design_bleeds;
drop policy if exists "print_team_delete" on public.design_bleeds;

create policy "print_team_select" on public.design_bleeds
  for select using (public.on_print_team(auth.uid()));
-- you may only write as yourself, so "who set this" stays honest
create policy "print_team_insert" on public.design_bleeds
  for insert with check (public.on_print_team(auth.uid()) and auth.uid() = user_id);
create policy "print_team_update" on public.design_bleeds
  for update using (public.on_print_team(auth.uid())) with check (auth.uid() = user_id);
create policy "print_team_delete" on public.design_bleeds
  for delete using (public.on_print_team(auth.uid()));


-- ---------- PART 3 of 3 : check ----------
-- Four print_team_* policies on the new table:
--   select policyname from pg_policies
--    where schemaname='public' and tablename='design_bleeds' order by policyname;
--
-- And what is currently bleeding differently to its sheet:
--   select path, mm, updated_at from public.design_bleeds order by path;
