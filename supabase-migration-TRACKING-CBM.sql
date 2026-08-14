-- ============================================================
-- TRACKING DETAIL, AND A FREIGHT RATE THAT IS ONLY TYPED ONCE
-- Run in the Supabase SQL Editor AFTER supabase-migration-SHARED-SHIPPING.sql.
-- Safe to run more than once, and safe to run as one batch.
--
-- Two small additions, both about not asking twice for something already
-- known.
-- ============================================================

-- ---------- PART 1 of 3 : what is coming, and who is bringing it ----------
-- A tracking number arrives before the parcel does, and usually before anyone
-- knows how many boxes it will turn out to be. Both are therefore nullable:
-- "not stated yet" is a real answer and is not the same as zero boxes.
alter table public.shipment_tracking
  add column if not exists est_box_qty      integer,
  add column if not exists logistic_company text;


-- ---------- PART 2 of 3 : the rate per CBM ----------
-- The freight forwarder's rate belongs to the forwarder, not to the box. It
-- was being keyed into every box measurement in turn, which is both tedious
-- and how boxes on one order end up costed at different rates by accident.
-- Held on the order, it is answered once when the purchase order is imported
-- and every box measured afterwards starts from it.
--
-- Existing orders get 0, which is what their boxes already default to, so
-- nothing that has been measured changes value.
alter table public.shipments
  add column if not exists cbm_rate_rmb numeric not null default 0;


-- ---------- PART 3 of 3 : check ----------
-- The new tracking columns:
--   select tracking_number, eta, est_box_qty, logistic_company
--     from public.shipment_tracking order by created_at desc limit 20;
--
-- And the rate now carried by each order:
--   select order_id, rmb_rate, cbm_rate_rmb from public.shipments
--    where deleted_at is null order by sort_order;
--
-- No policy changes: both tables are already covered by the ship_team_*
-- policies, which are per table rather than per column.
