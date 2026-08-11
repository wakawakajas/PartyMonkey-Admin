-- ============================================================
-- SHARED STORE PICK UP — run AFTER supabase-migration-ACCESS.sql.
-- Safe to run more than once.
--
-- Written as plain statements with no DO blocks, so it can be pasted in
-- pieces and a truncated paste fails loudly instead of half-applying.
--
-- Pick ups stop being private to whoever typed them and become one shared
-- list: anyone granted Store Pick Up sees the same rows, and a change by any
-- of them notifies the others. Orders, shipping and print templates are
-- untouched and stay private per account.
-- ============================================================

-- ---------- PART 1 of 3 : who counts as being on the team ----------
-- security definer so the policies below can read profiles without tripping
-- over the row level security on profiles itself.
--
-- can_pickup alone decides this — being an admin does not quietly put you on
-- the team, or an admin would keep getting pick up notifications after the
-- section was switched off for them. An admin can always grant it back to
-- themselves from Users & access, so there is nothing to lock yourself out of.
create or replace function public.on_pickup_team(uid uuid)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select coalesce((select can_pickup from public.profiles where user_id = uid), false);
$func$;


-- ---------- PART 2 of 3 : one shared pick up list ----------
drop policy if exists "own_select"  on public.pickups;
drop policy if exists "own_insert"  on public.pickups;
drop policy if exists "own_update"  on public.pickups;
drop policy if exists "own_delete"  on public.pickups;
drop policy if exists "team_select" on public.pickups;
drop policy if exists "team_insert" on public.pickups;
drop policy if exists "team_update" on public.pickups;
drop policy if exists "team_delete" on public.pickups;

create policy "team_select" on public.pickups
  for select using (public.on_pickup_team(auth.uid()));

-- user_id still records who added it, so the row keeps its authorship
create policy "team_insert" on public.pickups
  for insert with check (public.on_pickup_team(auth.uid()) and auth.uid() = user_id);

create policy "team_update" on public.pickups
  for update using (public.on_pickup_team(auth.uid()));

create policy "team_delete" on public.pickups
  for delete using (public.on_pickup_team(auth.uid()));


-- ---------- PART 3 of 3 : photos and notifications ----------
-- Pick up photos live at <uid>/pickup-<row>/<file>, so the existing
-- "your own folder only" rule would hide them from everyone else. This adds a
-- second, narrower permission for pick up photos alone — shipment and box
-- photos stay private to whoever uploaded them.
drop policy if exists "pickup_photos_team_select" on storage.objects;
drop policy if exists "pickup_photos_team_delete" on storage.objects;

create policy "pickup_photos_team_select" on storage.objects
  for select using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[2] like 'pickup-%'
    and public.on_pickup_team(auth.uid())
  );

create policy "pickup_photos_team_delete" on storage.objects
  for delete using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[2] like 'pickup-%'
    and public.on_pickup_team(auth.uid())
  );

-- the notify function reads every team member's devices, which needs to see
-- past each account's own rows
drop policy if exists "push_team_select" on public.push_subscriptions;

create policy "push_team_select" on public.push_subscriptions
  for select using (auth.uid() = user_id or public.on_pickup_team(auth.uid()));

-- safety net: make sure the owner is still on the team after the switch
update public.profiles set can_pickup = true
 where email = 'jasminewaka21@gmail.com';
