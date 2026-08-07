-- Migration: allocation-mode toggles for the China fee and box freight.
-- Run ONCE in the Supabase SQL Editor. Clear the box first (Ctrl+A, Delete),
-- paste this whole file, then Run. Expect "Success. No rows returned".

-- 'ordered' = split the China fee across every ordered piece (default,
-- matches previous behaviour). 'arrived' = only across pieces already
-- ticked into a box.
alter table public.shipments
  add column if not exists china_basis text not null default 'ordered';

-- 'box' = each box's freight is charged only to the pieces inside it
-- (default, matches previous behaviour). 'shared' = pool every box's
-- freight and average it over all ordered pieces.
alter table public.shipments
  add column if not exists freight_mode text not null default 'box';

-- keep the columns to known values so a bad write can't silently change
-- how money is allocated
alter table public.shipments drop constraint if exists shipments_china_basis_chk;
alter table public.shipments add constraint shipments_china_basis_chk
  check (china_basis in ('ordered','arrived'));

alter table public.shipments drop constraint if exists shipments_freight_mode_chk;
alter table public.shipments add constraint shipments_freight_mode_chk
  check (freight_mode in ('box','shared'));
