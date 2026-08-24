-- ============================================================
-- BUNDLE SKU — two shops, two combination lists
-- Run in the Supabase SQL Editor AFTER supabase-migration-BUNDLE-ORDERS.sql.
-- Safe to run more than once. Plain statements, no DO blocks.
--
-- PartyMonkey and Plant Talks sell different combinations, and the same name
-- can mean a different box in each. So a combination belongs to a shop, and
-- an order is matched against that shop's list alone. Which shop an order is
-- for is read off its file name -- PT in the name is Plant Talks -- the same
-- rule the Daily Pick List has always used, because it is the same file
-- naming and one rule is easier to keep than two.
-- ============================================================

alter table public.bundle_skus   add column if not exists shop text not null default 'partymonkey';
alter table public.bundle_orders add column if not exists shop text not null default 'partymonkey';

-- The name was unique over the whole table, which made one shop's list able
-- to overwrite the other's rows on upload. It is the pair that is unique now.
alter table public.bundle_skus drop constraint if exists bundle_skus_name_key;
create unique index if not exists bundle_skus_shop_name_key on public.bundle_skus(shop, name);
create index if not exists bundle_skus_shop_idx on public.bundle_skus(shop);
