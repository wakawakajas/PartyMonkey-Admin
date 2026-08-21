-- ============================================================
-- DAILY PICK LIST: WORDS SAID A DIFFERENT WAY
-- Run in the Supabase SQL Editor AFTER supabase-migration-PICK-LIST-HIDDEN2.sql
-- (which is where the other two word lists live).
-- Safe to run more than once.
--
-- A phone reads a SKU name the way its own voice happens to read it, and on
-- some names that is wrong in a way nobody can work around: "PT" comes out as
-- a word, "1m" as "one em", a Chinese name as silence. This list says how a
-- word should be said instead, and the voice says that in its place.
--
-- It is not per shop and not per phone: how a name is pronounced is a fact
-- about the name, and a picker should not have to teach their own phone.
-- ============================================================

create table if not exists public.pick_list_said_as (
  id uuid primary key default gen_random_uuid(),
  -- nullable, the same as the other two lists: an entry should not go because
  -- the person who typed it has left
  user_id uuid references auth.users(id) on delete set null,
  -- what is written on the sheet
  term text not null check (length(trim(term)) > 0),
  -- and what the voice should say when it meets it
  say_as text not null default '',
  created_at timestamptz not null default now()
);

-- one ruling per word; a second one would be a coin toss every time it is read
create unique index if not exists pick_list_said_as_term_key
  on public.pick_list_said_as(lower(trim(term)));

alter table public.pick_list_said_as enable row level security;

drop policy if exists "team_select" on public.pick_list_said_as;
drop policy if exists "team_insert" on public.pick_list_said_as;
drop policy if exists "team_update" on public.pick_list_said_as;
drop policy if exists "team_delete" on public.pick_list_said_as;

create policy "team_select" on public.pick_list_said_as
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.pick_list_said_as
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.pick_list_said_as
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.pick_list_said_as
  for delete using (public.on_team(auth.uid()));

alter table public.pick_list_said_as replica identity full;
