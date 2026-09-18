-- ============================================================
-- SWITCHING REPLIES OFF
-- Run in the Supabase SQL Editor AFTER supabase-migration-REPLIES.sql.
-- Safe to run more than once.
--
-- The switch on the Replies tile. While off_at is set the shop PC reads
-- nothing out of DuoKe and types nothing back: messages stay in DuoKe, and a
-- reply already approved waits in Pigu until it is switched on again. It is a
-- stamp on the one reply_sync row because the PC is on its own clock and asks
-- that row every pass anyway -- so the switch works from a phone, with nobody
-- going near the PC.
-- ============================================================

alter table public.reply_sync
  add column if not exists off_at timestamptz;
alter table public.reply_sync
  add column if not exists off_by text not null default '';

-- ---------- check ----------
--   select off_at, off_by from public.reply_sync;
