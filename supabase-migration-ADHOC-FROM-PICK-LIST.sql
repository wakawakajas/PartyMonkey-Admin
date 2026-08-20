-- ============================================================
-- AD-HOC RAISED OFF A DAILY PICK LIST
-- Run in the Supabase SQL Editor AFTER supabase-migration-ADHOC.sql,
-- supabase-migration-STORES.sql and supabase-migration-DAILY-PICK-LIST.sql.
-- Safe to run more than once.
--
-- Marking a line Ad-hoc while picking the morning's sheet now puts it on the
-- Ad-Hoc Picking page as a real pick list, so whoever deals with it does so
-- where every other ad-hoc job already lives.
--
-- It is still one-off work: the SKU is NOT written to warehouse_skus. The
-- catalogue is what the shop stocks, and a line that came up short one
-- morning has no business being added to it. The name, the quantity, the
-- remark and the picture travel with the line instead.
-- ============================================================

-- ---------- which sheet the list came off ----------
-- Null for every list somebody made by hand, which is all of them so far.
-- on delete set null rather than cascade: the sheet is cleared out five days
-- after it is completed, and the ad-hoc work raised off it is not finished
-- just because the sheet has gone.
alter table public.adhoc_orders
  add column if not exists pick_list_id uuid
    references public.pick_lists(id) on delete set null;

-- asked once each time a line is raised, to find the list rather than make
-- a second one
create index if not exists adhoc_orders_pick_list_idx
  on public.adhoc_orders(pick_list_id)
  where pick_list_id is not null;


-- ---------- which line, and what it looked like ----------
-- Taking Ad-hoc back off a line has to remove the job it created, so the job
-- remembers the line it came from. Again set null rather than cascade: once
-- the sheet is gone the job stands on its own and should not go with it.
alter table public.adhoc_items
  add column if not exists pick_item_id uuid
    references public.pick_list_items(id) on delete set null;

create index if not exists adhoc_items_pick_item_idx
  on public.adhoc_items(pick_item_id)
  where pick_item_id is not null;

-- The line's own picture, as against warehouse_skus.photo_path, which is the
-- catalogue's picture of a SKU the shop stocks. This one belongs to this job
-- and nothing else, which is what "one-off" means here.
--
-- It points at the sheet's own thumbnail rather than a copy of it, so it goes
-- when the sheet is cleared out five days after completion — by which time
-- the job it illustrates is long done, and the name, quantity and remark
-- stay either way.
alter table public.adhoc_items
  add column if not exists photo_path text;
