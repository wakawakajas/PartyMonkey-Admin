-- ============================================================
-- MULTIPLE QTY ON AN ORDER LINE
-- Run in the Supabase SQL Editor AFTER supabase-migration-RUN-THIS.sql.
-- Safe to run more than once, and safe to run as one batch.
--
-- A line bought by the carton becomes a line counted in pieces: 5 cartons at
-- ¥240 becomes 35 pieces at ¥34.285714…, with the goods total unchanged.
--
-- The line stores the FINAL figures — qty and unit_price_rmb are the pieces
-- and the price per piece. Everything that already reads a line (the costing,
-- the PO export, the arrived counts, the box allocations) therefore keeps
-- working with no change at all, and only breaking down or combining writes
-- anything. The two columns below exist so it can be undone.
-- ============================================================

-- How many pieces one purchased unit holds. 1 means the line has not been
-- broken down, which is every line that exists today.
alter table public.shipment_items
  add column if not exists qty_per_unit integer not null default 1;

-- The price of one whole unit, kept from before the division. Combining back
-- restores this value rather than multiplying the divided price back up:
-- 240/7 in binary floating point does not return to exactly 240, and a line
-- that has been broken down and recombined twice should read exactly as it
-- did when it was typed.
alter table public.shipment_items
  add column if not exists unit_price_base_rmb numeric;

-- a factor of zero or less would divide the price into nothing
alter table public.shipment_items
  drop constraint if exists shipment_items_qty_per_unit_positive;
alter table public.shipment_items
  add constraint shipment_items_qty_per_unit_positive check (qty_per_unit >= 1);


-- ---------- check ----------
-- Every existing line reads as not broken down:
--   select count(*) filter (where qty_per_unit = 1) as plain,
--          count(*) filter (where qty_per_unit > 1) as broken_down
--     from public.shipment_items;
