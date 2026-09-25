-- ---- freight chosen per box, not per order ------------------------------
-- A box is either charged only to the pieces inside it ('box') or pooled with
-- the order's other shared boxes and spread over the pieces that arrived in
-- them ('shared'). Every box already made takes the setting its order had, so
-- no order's costs move when this runs.
alter table public.shipment_boxes
  add column if not exists freight_mode text
    check (freight_mode in ('box','shared'));

update public.shipment_boxes b
   set freight_mode = case when s.freight_mode = 'shared' then 'shared' else 'box' end
  from public.shipments s
 where s.id = b.shipment_id
   and b.freight_mode is null;
