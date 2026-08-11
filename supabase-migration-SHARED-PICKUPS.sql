-- ============================================================
-- SHARED STORE PICK UP — run AFTER supabase-migration-ACCESS.sql.
-- Safe to run more than once.
--
--   1. Dashboard -> SQL Editor -> New query
--   2. Click in the box, Ctrl+A then Delete (make sure it is EMPTY)
--   3. Paste this whole file, click Run
--   4. Expect: "Success. No rows returned"
--
-- Pick ups stop being private to whoever typed them and become one shared
-- list: anyone granted Store Pick Up sees the same rows, and a change by any
-- of them notifies the others. Orders, shipping and print templates are
-- untouched and stay private per account.
-- ============================================================

-- guard: this file leans on the profiles table from the access migration
do $$
begin
  if to_regclass('public.profiles') is null then
    raise exception 'Run supabase-migration-ACCESS.sql first — profiles is missing.';
  end if;
end $$;

-- "is this person on the pick up team" — security definer so the policies can
-- read profiles without tripping over profiles' own row level security
create or replace function public.on_pickup_team(uid uuid)
returns boolean language sql security definer stable set search_path = public as $$
  select coalesce((select can_pickup or is_admin from public.profiles where user_id = uid), false);
$$;

do $$
begin
  execute 'drop policy if exists "own_select"  on public.pickups';
  execute 'drop policy if exists "own_insert"  on public.pickups';
  execute 'drop policy if exists "own_update"  on public.pickups';
  execute 'drop policy if exists "own_delete"  on public.pickups';
  execute 'drop policy if exists "team_select" on public.pickups';
  execute 'drop policy if exists "team_insert" on public.pickups';
  execute 'drop policy if exists "team_update" on public.pickups';
  execute 'drop policy if exists "team_delete" on public.pickups';
  -- one list, shared by the team
  execute 'create policy "team_select" on public.pickups for select
             using (public.on_pickup_team(auth.uid()))';
  -- user_id still records who added it, so the row keeps its authorship
  execute 'create policy "team_insert" on public.pickups for insert
             with check (public.on_pickup_team(auth.uid()) and auth.uid() = user_id)';
  execute 'create policy "team_update" on public.pickups for update
             using (public.on_pickup_team(auth.uid()))';
  execute 'create policy "team_delete" on public.pickups for delete
             using (public.on_pickup_team(auth.uid()))';
end $$;

-- Pick up photos live at <uid>/pickup-<row>/<file>, so the existing
-- "your own folder only" rule would hide them from everyone else. This adds a
-- second, narrower permission for pick up photos alone — shipment and box
-- photos stay private to whoever uploaded them.
drop policy if exists "pickup_photos_team_select" on storage.objects;
drop policy if exists "pickup_photos_team_delete" on storage.objects;

create policy "pickup_photos_team_select" on storage.objects for select
  using (bucket_id = 'shipment-photos'
         and (storage.foldername(name))[2] like 'pickup-%'
         and public.on_pickup_team(auth.uid()));
create policy "pickup_photos_team_delete" on storage.objects for delete
  using (bucket_id = 'shipment-photos'
         and (storage.foldername(name))[2] like 'pickup-%'
         and public.on_pickup_team(auth.uid()));

-- the notify function reads every team member's devices, which needs to see
-- past each account's own rows
drop policy if exists "push_team_select" on public.push_subscriptions;
create policy "push_team_select" on public.push_subscriptions for select
  using (auth.uid() = user_id or public.on_pickup_team(auth.uid()));

-- safety net: make sure the owner is still on the team after the switch
update public.profiles
   set can_pickup = true
 where email = 'jasminewaka21@gmail.com';
