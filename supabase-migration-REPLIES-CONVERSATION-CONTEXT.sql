-- ============================================================
-- FIX: Link reply examples to their conversation context
-- This prevents pulling examples from the wrong customer's thread
-- Run in the Supabase SQL Editor AFTER supabase-migration-REPLIES.sql
-- Safe to run more than once.
--
-- The bug: reply_examples was picking examples by word-matching only,
-- without checking which conversation they came from. This caused:
-- - Examples from Customer A's thread being used for Customer B's reply
-- - Wrong context being shown to different buyers
-- - Queue getting stuck with nonsensical drafts
--
-- The fix: Link each approved example back to its message thread via
-- chat_key, so the pick() function can filter by conversation context.
-- ============================================================

-- Add conversation tracking to reply_examples (nullable first)
alter table public.reply_examples
  add column if not exists chat_key text default '';

alter table public.reply_examples
  add column if not exists buyer text default '';

-- ============================================================
-- MIGRATION SCRIPT FOR EXISTING DATA
-- If you have existing reply_examples, populate chat_key and buyer
-- from the reply_messages they were approved for (the one with matching text)
-- ============================================================

-- For each example, find and link the original message it was based on
-- This matches by buyer_text and reply_text to find the corresponding message
update public.reply_examples ex
  set chat_key = coalesce((
    select m.chat_key from public.reply_messages m
    where m.reply = ex.reply_text
      and m.user_id = ex.user_id
    order by m.answered_at desc
    limit 1
  ), ''),
  buyer = coalesce((
    select m.buyer from public.reply_messages m
    where m.reply = ex.reply_text
      and m.user_id = ex.user_id
    order by m.answered_at desc
    limit 1
  ), '')
  where (chat_key is null or chat_key = '')
    and (buyer is null or buyer = '');

-- Create index for filtering by conversation when picking examples
create index if not exists reply_examples_context_idx
  on public.reply_examples (chat_key, created_at desc);

-- Create index for filtering by buyer
create index if not exists reply_examples_buyer_idx
  on public.reply_examples (buyer, created_at desc);

-- ============================================================
-- CHECK: See if migration populated the data
-- select count(*) as total,
--        count(chat_key) filter (where chat_key != '') as with_context,
--        count(buyer) filter (where buyer != '') as with_buyer
-- from public.reply_examples;
-- ============================================================
