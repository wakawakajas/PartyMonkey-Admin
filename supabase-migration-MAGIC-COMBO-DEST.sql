-- ============================================================
-- MAGIC CREATE — ROUTING CAN SEND A SKU TO CUSTOM GIFT TAG 2
-- Run in the Supabase SQL Editor AFTER supabase-migration-MAGIC-PLACECARD-DEST.sql.
-- Safe to run more than once.
--
-- Custom Gift Tag 2 (page 'combo') joins the gift tag pages a SKU can be
-- routed to. The constraint is restated in full, as the file before it did.
-- No new table, so no grants are needed.
-- ============================================================

alter table public.magic_routes drop constraint if exists magic_routes_dest_check;
alter table public.magic_routes
  add constraint magic_routes_dest_check
  check (dest in ('print','festive','custom','sticker','combo','placeCard','copy','skip'));
