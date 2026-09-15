-- ============================================================
-- STICKER WORDS — what is taken off a bundle's name before it is printed
-- Run in the Supabase SQL Editor AFTER supabase-migration-BUNDLE-RULES.sql.
-- Safe to run more than once.
--
-- A sticker carries the Bundle SKU's own name, less a few words that mean
-- nothing on the tape: "Seeds-Mimosa (10pc)" comes out as "Mimosa (10pc)".
-- Kept apart from seed_words on purpose. That list decides what a Plant
-- Talks order packs, and a word like DIY belongs on it there while it
-- belongs on the sticker here.
--
-- One comma-separated field, edited as a list on Bundle SKU Settings, the
-- same way the other rules in this row are.
-- ============================================================

alter table public.bundle_settings
  add column if not exists label_strip_words text not null default 'seed, seeds, popular';
