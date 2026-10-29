-- ============================================================
-- HOME BANNERS: ONLY AN ADMIN MAY CHANGE THEM
-- Run in the Supabase SQL Editor AFTER supabase-migration-HOME-BANNERS.sql
-- and supabase-migration-ACCESS.sql (which made public.is_admin).
-- Safe to run more than once.
--
-- Everyone on the team still sees the banners. Putting one up, swapping it
-- or taking it down is for admins only, held here and not just in the app.
-- ============================================================

-- ---------- PART 1 of 2 : the banner rows ----------
drop policy if exists "home_banners_team_insert" on public.home_banners;
drop policy if exists "home_banners_team_update" on public.home_banners;
drop policy if exists "home_banners_team_delete" on public.home_banners;
drop policy if exists "home_banners_admin_insert" on public.home_banners;
drop policy if exists "home_banners_admin_update" on public.home_banners;
drop policy if exists "home_banners_admin_delete" on public.home_banners;
create policy "home_banners_admin_insert" on public.home_banners
  for insert with check (public.is_admin(auth.uid()));
create policy "home_banners_admin_update" on public.home_banners
  for update using (public.is_admin(auth.uid()));
create policy "home_banners_admin_delete" on public.home_banners
  for delete using (public.is_admin(auth.uid()));


-- ---------- PART 2 of 2 : the banner photos ----------
drop policy if exists "home_banner_photos_insert" on storage.objects;
drop policy if exists "home_banner_photos_delete" on storage.objects;
create policy "home_banner_photos_insert" on storage.objects
  for insert with check (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[1] = 'home-banners'
    and public.is_admin(auth.uid())
  );
create policy "home_banner_photos_delete" on storage.objects
  for delete using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[1] = 'home-banners'
    and public.is_admin(auth.uid())
  );


-- ---------- check ----------
--   select policyname, cmd from pg_policies
--    where policyname like 'home_banner%' order by 1;
