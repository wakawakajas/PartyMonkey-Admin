-- ============================================================
-- STICKER WORDS PER SHOP
-- Run in the Supabase SQL Editor AFTER supabase-migration-STICKER-WORDS.sql.
-- Safe to run more than once.
--
-- The two shops name their Bundle SKUs differently, so each takes its own
-- words off a sticker. label_strip_words stays PartyMonkey's; Plant Talks gets
-- label_strip_words_pt, starting as a copy of the list both shared.
-- ============================================================

alter table public.bundle_settings
  add column if not exists label_strip_words text not null default 'seed, seeds, popular';

alter table public.bundle_settings
  add column if not exists label_strip_words_pt text;

update public.bundle_settings
  set label_strip_words_pt = label_strip_words
  where label_strip_words_pt is null;

alter table public.bundle_settings
  alter column label_strip_words_pt set default 'seed, seeds, popular';
