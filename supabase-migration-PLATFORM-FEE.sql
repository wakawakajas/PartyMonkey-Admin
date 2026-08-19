-- ---- the platform's own cut, charged before World First's ----------------
-- What is paid through World First is the goods, the China-side delivery and
-- the platform fee on top of them; their percentage is taken on that sum, and
-- GST on all of it. Splitting the old single charge in two is what lets each
-- be charged on the right figure — see the Xero export, which has always
-- billed these as two separate lines.
alter table public.shipments
  add column if not exists platform_fee_pct numeric not null default 0.2;

-- new orders get 0.8 rather than the old combined 1
alter table public.shipments alter column service_fee_pct set default 0.8;

-- Orders still sitting on the old combined 1% move to the split. An order
-- someone has typed a different number into is left exactly as it is.
update public.shipments set service_fee_pct = 0.8 where service_fee_pct = 1;
