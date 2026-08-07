-- Migration: China domestic shipping fee + item photos.
-- Run ONCE in the Supabase SQL Editor. Clear the box first (Ctrl+A, Delete),
-- paste this whole file, then Run. Expect "Success. No rows returned".

-- flat China-side delivery fee for the order, split across every piece
alter table public.shipments      add column if not exists china_fee_rmb numeric not null default 0;
-- storage path of the item's photo (null = no photo)
alter table public.shipment_items add column if not exists photo_path text;

-- private bucket for item photos; files live under {user_id}/{item_id}/...
insert into storage.buckets (id, name, public)
values ('shipment-photos', 'shipment-photos', false)
on conflict (id) do nothing;

-- only the owner of the top-level folder (their auth uid) can touch a file
drop policy if exists "shipment_photos_select" on storage.objects;
drop policy if exists "shipment_photos_insert" on storage.objects;
drop policy if exists "shipment_photos_update" on storage.objects;
drop policy if exists "shipment_photos_delete" on storage.objects;

create policy "shipment_photos_select" on storage.objects for select
  using (bucket_id = 'shipment-photos' and (storage.foldername(name))[1] = auth.uid()::text);

create policy "shipment_photos_insert" on storage.objects for insert
  with check (bucket_id = 'shipment-photos' and (storage.foldername(name))[1] = auth.uid()::text);

create policy "shipment_photos_update" on storage.objects for update
  using (bucket_id = 'shipment-photos' and (storage.foldername(name))[1] = auth.uid()::text);

create policy "shipment_photos_delete" on storage.objects for delete
  using (bucket_id = 'shipment-photos' and (storage.foldername(name))[1] = auth.uid()::text);
