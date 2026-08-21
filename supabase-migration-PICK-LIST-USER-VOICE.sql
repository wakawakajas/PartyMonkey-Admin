-- ============================================================
-- DAILY PICK LIST: A VOICE CHOSEN FOR ONE PERSON
-- Run in the Supabase SQL Editor AFTER supabase-migration-PICK-LIST-VOICE.sql.
-- Safe to run more than once.
--
-- The voice is a setting of the shop: everyone picking PartyMonkey's sheets
-- hears the same one, which is right, because it is how that floor works.
--
-- This is the exception. Somebody who cannot make out the shop's voice over
-- the machinery, or who simply picks better with a different one, can be
-- given their own — by an admin, on Users & access, rather than by finding
-- Settings themselves. Null means "whatever the shop is set to", which is
-- what everybody is until somebody says otherwise.
--
-- One column rather than four: it holds the whole choice — the accent, the
-- voice's name, the pitch and the speed — as one value, because those four
-- together are what a voice is and a half-applied one is nobody's choice.
-- ============================================================

alter table public.profiles
  add column if not exists pl_voice text;
