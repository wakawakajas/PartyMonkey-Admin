-- ============================================================
-- AD-HOC PICKING: SKU PHOTOS, PICKED QUANTITY, LOCATION REMARKS
-- Run in the Supabase SQL Editor AFTER supabase-migration-ADHOC.sql.
-- Safe to run more than once.
-- ============================================================

-- a picture of the SKU, stored in the same bucket as everything else
alter table public.warehouse_skus add column if not exists photo_path text;

-- how many were actually found, and where they were
alter table public.adhoc_items add column if not exists qty_picked integer;
alter table public.adhoc_items add column if not exists location_remarks text not null default '';

-- SKU photos live at <uid>/sku/<name>.jpg, so the "your own folder only" rule
-- would hide them from the rest of the team the way pick up photos were
drop policy if exists "sku_photos_team_select" on storage.objects;
drop policy if exists "sku_photos_team_delete" on storage.objects;

create policy "sku_photos_team_select" on storage.objects
  for select using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[2] = 'sku'
    and public.on_team(auth.uid())
  );

create policy "sku_photos_team_delete" on storage.objects
  for delete using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[2] = 'sku'
    and public.on_team(auth.uid())
  );
