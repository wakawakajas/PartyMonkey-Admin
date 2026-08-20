-- ============================================================
-- DAILY PICK LIST: WHICH SKUs ARE LEFT OFF THE SHEET
-- Run in the Supabase SQL Editor AFTER supabase-migration-DAILY-PICK-LIST.sql.
-- Safe to run more than once.
--
-- Some lines on the morning's sheet are not picked off a shelf at all —
-- anything custom is printed to order — so they are hidden by default and
-- left out of the count, the progress bar and what Complete List waits for.
--
-- Which ones those are was the word "Custom" written into the app. It is a
-- list kept here instead, so the floor can add to it without waiting for a
-- release. A term matches anywhere in the SKU and ignores case, exactly as
-- the built-in one did.
-- ============================================================

create table if not exists public.pick_list_hidden_skus (
  id uuid primary key default gen_random_uuid(),
  -- Nullable, unlike everywhere else in this app: the seed below belongs to
  -- nobody in particular, and a term should not disappear because the person
  -- who happened to type it has left.
  user_id uuid references auth.users(id) on delete set null,
  term text not null check (length(trim(term)) > 0),
  created_at timestamptz not null default now()
);

-- the same word twice is not two rules
create unique index if not exists pick_list_hidden_skus_term_key
  on public.pick_list_hidden_skus(lower(trim(term)));

alter table public.pick_list_hidden_skus enable row level security;

drop policy if exists "team_select" on public.pick_list_hidden_skus;
drop policy if exists "team_insert" on public.pick_list_hidden_skus;
drop policy if exists "team_update" on public.pick_list_hidden_skus;
drop policy if exists "team_delete" on public.pick_list_hidden_skus;

-- one list for the whole floor, the same as the sheets it applies to
create policy "team_select" on public.pick_list_hidden_skus
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.pick_list_hidden_skus
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.pick_list_hidden_skus
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.pick_list_hidden_skus
  for delete using (public.on_team(auth.uid()));

-- so a screen that is open somewhere else keeps up
alter table public.pick_list_hidden_skus replica identity full;

-- What the app did before this table existed, written down as a row so that
-- nothing changes on the day it is run. Only ever on a table that is empty:
-- somebody who has cleared the list wants it cleared, and running the file
-- again must not put "Custom" back under them.
insert into public.pick_list_hidden_skus (term)
select 'Custom'
 where not exists (select 1 from public.pick_list_hidden_skus);
