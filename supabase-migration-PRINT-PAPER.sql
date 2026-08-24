-- ============================================================
-- PAPER TYPE AND GSM
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql and
-- supabase-migration-ADHOC.sql (which is where public.on_team comes from).
-- Safe to run more than once, and safe to run as one batch — the Editor
-- wraps a pasted file in a transaction, so nothing here may raise on a
-- second run or it would undo everything above it.
--
-- What a job is printed on now names the file it comes out as, so the two
-- lists behind those dropdowns are shop knowledge rather than one person's:
-- anybody who can open Custom Print Template can add to them, correct them,
-- and take an entry off. They are kept here rather than in a browser so the
-- floor and the office are choosing from the same words.
-- ============================================================

-- ---------- PART 1 of 3 : the list ----------
-- One table for both dropdowns rather than two near-identical ones. `kind`
-- is what a row is for, and the app never asks for both at once.
create table if not exists public.print_papers (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  kind text not null check (kind in ('paper','gsm')),
  value text not null,
  -- the order they are offered in: the ones reached for most sit at the top,
  -- the same as the template list does it
  sort_order integer not null default 0,
  created_at timestamptz not null default now()
);

-- Two people adding "Matte" on the same morning should end up with one entry,
-- not two that look identical in a dropdown. Case-insensitive, because
-- "matte" and "Matte" are the same paper.
create unique index if not exists print_papers_kind_value_idx
  on public.print_papers(kind, lower(value));
create index if not exists print_papers_kind_idx
  on public.print_papers(kind, sort_order);

alter table public.print_papers enable row level security;


-- A Magic Create line that goes through Custom Template is a job like any
-- other, so it carries the same two answers. Per line rather than per batch:
-- one morning's orders are not all on one paper.
alter table public.magic_items
  add column if not exists paper_type text not null default '';
alter table public.magic_items
  add column if not exists gsm text not null default '';


-- ---------- PART 2 of 3 : shared with the team ----------
-- Deliberately wider than "your own": a paper somebody added last week is the
-- same paper this week, and an entry only one person can correct is an entry
-- that stays wrong.
drop policy if exists "team_select" on public.print_papers;
drop policy if exists "team_insert" on public.print_papers;
drop policy if exists "team_update" on public.print_papers;
drop policy if exists "team_delete" on public.print_papers;

create policy "team_select" on public.print_papers
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.print_papers
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.print_papers
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.print_papers
  for delete using (public.on_team(auth.uid()));


-- ---------- PART 3 of 3 : something to start from ----------
-- An empty dropdown on the first morning is a screen nobody can get past, so
-- the commonest few are put in. They are ordinary rows: rename them, reorder
-- them, or delete the lot from the settings screen.
insert into public.print_papers (user_id, kind, value, sort_order)
select auth.uid(), v.kind, v.value, v.ord
  from (values
    ('paper','Matte',      0),
    ('paper','Gloss',      1),
    ('paper','Art Card',   2),
    ('paper','Kraft',      3),
    ('gsm','157',          0),
    ('gsm','200',          1),
    ('gsm','250',          2),
    ('gsm','300',          3),
    ('gsm','350',          4)
  ) as v(kind, value, ord)
 where auth.uid() is not null
   and not exists (select 1 from public.print_papers p
                    where p.kind = v.kind and lower(p.value) = lower(v.value));
