-- ============================================================
-- SEARCH WAREHOUSE MAY NOT CHANGE SKU PHOTOS
-- Run in the Supabase SQL Editor. Safe to run more than once.
--
-- set_item_photo let anyone on the team change or clear an SKU photo, even
-- without access to Procurement & Shipping. The app no longer uses it, and
-- the SKU photo is changed only in Procurement. Box photos are unaffected
-- (set_box_photo stays).
-- ============================================================

drop function if exists public.set_item_photo(uuid, text);

-- check: should return no rows
--   select proname from pg_proc where proname = 'set_item_photo';
