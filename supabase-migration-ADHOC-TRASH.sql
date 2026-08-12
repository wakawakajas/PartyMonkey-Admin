-- ============================================================
-- RECENTLY DELETED FOR PICK LISTS
-- Run in the Supabase SQL Editor AFTER supabase-migration-ADHOC.sql.
-- Safe to run more than once.
--
-- Deleting a pick list stops throwing it away and instead stamps the time,
-- exactly as shipments already work. The app hides anything stamped, offers it
-- back for 24 hours, and destroys it for good after that.
--
-- Nothing is needed for the lines on a list: adhoc_items already cascades from
-- adhoc_orders, so they go when the row itself is finally deleted, and they
-- come back untouched if the list is restored instead.
-- ============================================================

alter table public.adhoc_orders add column if not exists deleted_at timestamptz;

-- the pick list screen asks for "not deleted" on every load, and the bin asks
-- for the opposite, so the column is worth an index
create index if not exists adhoc_orders_deleted_idx on public.adhoc_orders(deleted_at);
