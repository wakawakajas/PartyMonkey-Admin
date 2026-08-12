-- ============================================================
-- TWO STORES IN AD-HOC PICKING: PartyMonkey and Plant Talks
-- Run in the Supabase SQL Editor AFTER supabase-migration-ADHOC.sql.
-- Safe to run more than once.
--
-- Each store keeps its own SKUs and its own pick lists. Everything that
-- exists today becomes PartyMonkey's, since that is where it was entered.
-- ============================================================

alter table public.warehouse_skus add column if not exists store text not null default 'partymonkey';
alter table public.adhoc_orders   add column if not exists store text not null default 'partymonkey';
alter table public.adhoc_items    add column if not exists store text not null default 'partymonkey';

-- A name only has to be unique within its own store — both shops may well
-- stock something called "Green Shovel", and neither should block the other.
alter table public.warehouse_skus drop constraint if exists warehouse_skus_sku_key;
drop index if exists warehouse_skus_store_sku_key;
create unique index warehouse_skus_store_sku_key
  on public.warehouse_skus(store, sku);

create index if not exists warehouse_skus_store_idx on public.warehouse_skus(store);
create index if not exists adhoc_orders_store_idx   on public.adhoc_orders(store);
create index if not exists adhoc_items_store_idx    on public.adhoc_items(store);
