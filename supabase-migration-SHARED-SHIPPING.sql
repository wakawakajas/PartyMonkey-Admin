-- ============================================================
-- ONE SHARED PROCUREMENT & SHIPPING
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql.
-- Safe to run more than once, and safe to run as one batch.
--
-- Orders, their item lines, boxes, tracking and consolidations stop being
-- private to whoever typed them and become one shared set, the same decision
-- Store Pick Up and Ad-Hoc Picking already made. Everyone granted the
-- Procurement & Shipping section sees the same orders and can work on them.
--
-- Print templates are NOT touched. Those stay private to each account —
-- they are somebody's saved layouts, not the shop's records.
--
-- WHAT THIS CHANGES FOR YOU: an order entered by one person is now visible
-- to, and editable by, everyone with that section. There is no per-order
-- ownership left to fall back on. If that is not what you want, do not run
-- this file.
-- ============================================================

-- ---------- PART 1 of 4 : who counts as on the shipping team ----------
-- can_shipping alone decides it — being an admin does not quietly add you,
-- matching how on_pickup_team already behaves. An admin can always grant it
-- back to themselves from Users & access, so nothing can be locked away.
create or replace function public.on_shipping_team(uid uuid)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select coalesce((select can_shipping from public.profiles where user_id = uid), false);
$func$;


-- ---------- PART 2 of 4 : the tables ----------
-- user_id stays on every row and keeps being written. It is no longer what
-- decides who may read a row, but it still records who entered it, which is
-- worth having when two people are working the same list.
do $$
declare t text;
begin
  foreach t in array array['shipments','shipment_items','shipment_boxes',
                           'shipment_box_items','shipment_tracking','consolidations'] loop
    -- every name these tables have been given across the earlier migrations
    execute format('drop policy if exists "own_select" on public.%I', t);
    execute format('drop policy if exists "own_insert" on public.%I', t);
    execute format('drop policy if exists "own_update" on public.%I', t);
    execute format('drop policy if exists "own_delete" on public.%I', t);
    execute format('drop policy if exists "Users can view own shipments"   on public.%I', t);
    execute format('drop policy if exists "Users can insert own shipments" on public.%I', t);
    execute format('drop policy if exists "Users can update own shipments" on public.%I', t);
    execute format('drop policy if exists "Users can delete own shipments" on public.%I', t);
    execute format('drop policy if exists "ship_team_select" on public.%I', t);
    execute format('drop policy if exists "ship_team_insert" on public.%I', t);
    execute format('drop policy if exists "ship_team_update" on public.%I', t);
    execute format('drop policy if exists "ship_team_delete" on public.%I', t);

    execute format('create policy "ship_team_select" on public.%I for select
                      using (public.on_shipping_team(auth.uid()))', t);
    -- you may only file a row as yourself, so "who entered this" stays honest
    execute format('create policy "ship_team_insert" on public.%I for insert
                      with check (public.on_shipping_team(auth.uid()) and auth.uid() = user_id)', t);
    execute format('create policy "ship_team_update" on public.%I for update
                      using (public.on_shipping_team(auth.uid()))', t);
    execute format('create policy "ship_team_delete" on public.%I for delete
                      using (public.on_shipping_team(auth.uid()))', t);
  end loop;
end $$;


-- ---------- PART 3 of 4 : the photos ----------
-- Item and box photos live at <uid>/<row-id>/<file>, and the bucket's standing
-- rule is "your own folder only" — so a shared order would still have shown
-- everyone else's pictures as broken squares. This adds a second, narrower
-- permission for photos that belong to a shipment row.
--
-- security definer because the check reads the shipment tables, which are
-- private: it answers one question about one path and nothing else.
create or replace function public.is_shipping_photo(p text)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select exists (
           select 1 from public.shipment_items i
            where i.id::text = (storage.foldername(p))[2])
      or exists (
           select 1 from public.shipment_boxes b
            where 'box-' || b.id::text = (storage.foldername(p))[2]);
$func$;

drop policy if exists "shipping_photos_team_select" on storage.objects;
drop policy if exists "shipping_photos_team_delete" on storage.objects;

create policy "shipping_photos_team_select" on storage.objects
  for select using (
    bucket_id = 'shipment-photos'
    and public.on_shipping_team(auth.uid())
    and public.is_shipping_photo(name)
  );

-- replacing a photo on a shared order means removing the one that was there,
-- whoever uploaded it
create policy "shipping_photos_team_delete" on storage.objects
  for delete using (
    bucket_id = 'shipment-photos'
    and public.on_shipping_team(auth.uid())
    and public.is_shipping_photo(name)
  );


-- ---------- PART 4 of 4 : check it did what you wanted ----------
-- Run these afterwards. The first should list four ship_team_* policies for
-- each of the six tables and nothing named own_* or "Users can ...".
--
--   select tablename, policyname from pg_policies
--    where schemaname = 'public'
--      and tablename in ('shipments','shipment_items','shipment_boxes',
--                        'shipment_box_items','shipment_tracking','consolidations')
--    order by tablename, policyname;
--
-- And this says who can now see the lot:
--
--   select email, can_shipping from public.profiles order by email;
