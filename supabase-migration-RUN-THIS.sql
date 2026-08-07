-- ============================================================
-- COMBINED MIGRATION — run this ONE file and you are up to date.
-- Covers: China shipping fee, item photos, and the two allocation
-- toggles. Safe to run even if you already ran part of it before;
-- every statement checks before it changes anything.
--
-- HOW TO RUN
--   1. Supabase dashboard -> SQL Editor -> New query
--   2. Click in the box, Ctrl+A then Delete (make sure it is EMPTY)
--   3. Paste this whole file, click Run
--   4. Expect: "Success. No rows returned"
-- ============================================================

-- ---- columns on shipments ----
-- flat China-side delivery fee for the order
alter table public.shipments add column if not exists china_fee_rmb numeric not null default 0;
-- 'ordered' = split China fee across every ordered piece (default)
-- 'arrived' = only across pieces already ticked into a box
alter table public.shipments add column if not exists china_basis  text    not null default 'ordered';
-- 'box'    = each box's freight charged only to the pieces inside it (default)
-- 'shared' = pool every box's freight, averaged over all ordered pieces
alter table public.shipments add column if not exists freight_mode text    not null default 'box';

-- keep the mode columns to known values so a bad write can't silently
-- change how money gets allocated
alter table public.shipments drop constraint if exists shipments_china_basis_chk;
alter table public.shipments add  constraint shipments_china_basis_chk
  check (china_basis in ('ordered','arrived'));

alter table public.shipments drop constraint if exists shipments_freight_mode_chk;
alter table public.shipments add  constraint shipments_freight_mode_chk
  check (freight_mode in ('box','shared'));

-- ---- item photos ----
alter table public.shipment_items add column if not exists photo_path text;

-- private bucket; files live under {user_id}/{item_id}/...
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
