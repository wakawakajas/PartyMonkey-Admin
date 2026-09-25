-- ---- not-arrived pieces sent on with a later order ------------------------
-- A supplier often packs the pieces missing from one order in with the next,
-- so they share that order's freight. The line on the new order points back
-- at the line it came from, and carries what one piece of it already cost
-- there — goods, China fee, platform, World First and GST, but no freight.
-- Its own unit price stays 0, so the new order's goods and fees never count
-- money that was paid on the old one.
alter table public.shipment_items
  add column if not exists carried_from_item_id uuid
    references public.shipment_items(id) on delete set null,
  add column if not exists carried_unit_rmb numeric;

create index if not exists shipment_items_carried_from_idx
  on public.shipment_items(carried_from_item_id);
