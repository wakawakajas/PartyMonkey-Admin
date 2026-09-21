-- ============================================================
-- MAGIC CREATE — ROUTING CAN SEND A SKU TO PLACE CARD
-- Run in the Supabase SQL Editor AFTER supabase-migration-MAGIC-COPY.sql.
-- Safe to run more than once.
--
-- Place Card joins Print sheet as a destination a SKU can be routed to: a
-- line whose SKU is routed here opens as a Place Card line automatically,
-- the same way routing to Print sheet already opens one as Custom Template.
-- The constraint is restated in full — print/festive/custom/sticker/copy/skip
-- plus the new 'placeCard' — rather than only adding the new value, since
-- 'sticker' was never in the constraint this file replaces and needs to be
-- named here to keep working.
-- ============================================================

alter table public.magic_routes drop constraint if exists magic_routes_dest_check;
alter table public.magic_routes
  add constraint magic_routes_dest_check
  check (dest in ('print','festive','custom','sticker','placeCard','copy','skip'));
