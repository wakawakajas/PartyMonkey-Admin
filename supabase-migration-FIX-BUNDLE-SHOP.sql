-- ============================================================
-- Fix Plant Talks SKUs that were incorrectly set to PartyMonkey
-- Run in the Supabase SQL Editor AFTER supabase-migration-BUNDLE-SHOP.sql
-- Safe to run more than once. Plain statements, no DO blocks.
--
-- When BUNDLE-SHOP migration ran, it set all SKUs to 'partymonkey' by default.
-- This migration restores Plant Talks SKUs by:
-- 1. Finding SKUs used in orders with 'PT' in the title (Plant Talks pattern)
-- 2. Setting those SKUs to 'planttalks'
-- ============================================================

-- Step 1: Find which SKUs are used in Plant Talks orders (PT in title)
-- and update them to 'planttalks'
update public.bundle_skus
set shop = 'planttalks'
where id in (
  select distinct bji.bundle_id
  from public.bundle_job_items bji
  join public.bundle_jobs bj on bji.job_id = bj.id
  join public.bundle_orders bo on bj.order_id = bo.id
  where bo.title ilike '%PT%'
    or bo.title ilike '%plant talks%'
);

-- Step 2: Also accept orders set explicitly with shop field if available
update public.bundle_skus
set shop = 'planttalks'
where id in (
  select distinct bji.bundle_id
  from public.bundle_job_items bji
  join public.bundle_jobs bj on bji.job_id = bj.id
  join public.bundle_orders bo on bj.order_id = bo.id
  where bo.shop = 'planttalks'
);

-- Verify the updates
select shop, count(*) as count from public.bundle_skus group by shop;
