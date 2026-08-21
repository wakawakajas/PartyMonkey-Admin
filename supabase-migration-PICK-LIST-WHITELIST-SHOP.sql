-- ============================================================
-- DAILY PICK LIST: A WHITELIST PER SHOP
-- Run in the Supabase SQL Editor AFTER supabase-migration-PICK-LIST-TOPUP.sql.
-- Safe to run more than once.
--
-- The exceptions to the hidden words started as one list for both shops, on
-- the reasoning that a whole SKU name belongs to whichever shop sells it and
-- cannot collide with the other's. In practice the two floors are set up by
-- different people and neither wants to read the other's exceptions to find
-- their own — the same call the hidden words themselves came to.
-- ============================================================

-- Everything already on the list was entered before there were two, and
-- PartyMonkey is where it was entered.
alter table public.pick_list_whitelist_skus
  add column if not exists store text not null default 'partymonkey';

create index if not exists pick_list_whitelist_skus_store_idx
  on public.pick_list_whitelist_skus(store);

-- A name only has to be unique within its own shop now: both of them may well
-- want the same exception, and neither should block the other.
drop index if exists pick_list_whitelist_skus_term_key;
create unique index if not exists pick_list_whitelist_skus_store_term_key
  on public.pick_list_whitelist_skus(store, lower(trim(term)));
