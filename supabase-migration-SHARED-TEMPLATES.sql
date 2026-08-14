-- ============================================================
-- ONE SHARED SET OF PRINT TEMPLATES
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql.
-- Safe to run more than once, and safe to run as one batch.
--
-- Saved layouts stop being private to whoever made them. Everyone granted
-- Custom Print Template sees the same list and can load any of them — the
-- decision Store Pick Up, Ad-Hoc Picking and Procurement & Shipping have all
-- already made.
--
-- WHAT THIS CHANGES FOR YOU: a template saved by one person is now visible
-- to, and deletable by, everyone with that section. There is no per-template
-- ownership left to fall back on.
--
-- IMPORTANT: deploy the app change that goes with this. Until then the print
-- screen addresses templates by name, and two people saving the same name
-- would make Load fail and Delete remove both. The app that ships with this
-- migration addresses them by id instead.
-- ============================================================

-- ---------- PART 1 of 3 : who counts as on the print team ----------
-- can_print alone decides it, matching how on_pickup_team and
-- on_shipping_team already behave. Being an admin does not quietly add you.
create or replace function public.on_print_team(uid uuid)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select coalesce((select can_print from public.profiles where user_id = uid), false);
$func$;


-- ---------- PART 2 of 3 : the templates ----------
-- user_id stays on every row and keeps being written: it no longer decides
-- who may read a layout, but it still records who made it, which is what lets
-- the list say whose is whose when two people pick the same name.
drop policy if exists "Users can view own templates"   on public.templates;
drop policy if exists "Users can insert own templates" on public.templates;
drop policy if exists "Users can update own templates" on public.templates;
drop policy if exists "Users can delete own templates" on public.templates;
drop policy if exists "print_team_select" on public.templates;
drop policy if exists "print_team_insert" on public.templates;
drop policy if exists "print_team_update" on public.templates;
drop policy if exists "print_team_delete" on public.templates;

create policy "print_team_select" on public.templates
  for select using (public.on_print_team(auth.uid()));
-- you may only save as yourself, so "who made this" stays honest
create policy "print_team_insert" on public.templates
  for insert with check (public.on_print_team(auth.uid()) and auth.uid() = user_id);
create policy "print_team_update" on public.templates
  for update using (public.on_print_team(auth.uid()));
create policy "print_team_delete" on public.templates
  for delete using (public.on_print_team(auth.uid()));

-- The unique key stays (user_id, name) rather than becoming (name). Two people
-- may well have their own "A6 postcard", and forcing one to rename would be a
-- worse answer than showing both and saying whose is whose. Saving still
-- overwrites your own by that name and never touches anybody else's.


-- ---------- PART 3 of 3 : check ----------
-- Four print_team_* policies and nothing named "Users can ...":
--   select policyname from pg_policies
--    where schemaname='public' and tablename='templates' order by policyname;
--
-- And who can now see them all:
--   select email, can_print from public.profiles order by email;
