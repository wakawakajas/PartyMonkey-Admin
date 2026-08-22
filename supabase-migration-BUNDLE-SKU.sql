-- ============================================================
-- BUNDLE SKU
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql.
-- Safe to run more than once.
--
-- Gates the "Bundle SKU" home tile. This is only the interface gate,
-- same as every other section -- there is no Bundle SKU data yet.
-- ============================================================

-- defaults to false, so nobody gains it without being given it
alter table public.profiles
  add column if not exists can_bundle_sku boolean not null default false;

-- Who can open it — grant it from Users & access, or here:
--   update public.profiles set can_bundle_sku = true where email = 'someone@example.com';
