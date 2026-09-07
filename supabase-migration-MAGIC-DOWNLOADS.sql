-- ============================================================
-- MAGIC CREATE : how many times a line has been downloaded
-- Run in the Supabase SQL Editor after supabase-migration-MAGIC-CREATE.sql.
-- Safe to run more than once.
--
-- exported_at already says WHEN a line last went to a PDF. On a batch two
-- people are working, the question after that one is whether it has been out
-- before: a line downloaded twice is a line somebody was not sure about the
-- first time, and a reprint asked for and forgotten looks exactly like a line
-- nobody has touched. So the count is kept beside the stamp and shown on the
-- line — "Downloaded 1 time", "Downloaded 2 times".
--
-- Lines stamped before this column existed count as one: they did go out, and
-- the app reads a missing or zero count as once.
--
-- The app runs without this. A write naming a column the database has not
-- been given is retried without it, so a batch worked before this migration
-- is run still keeps its download stamps — it simply cannot count them.
-- ============================================================

alter table public.magic_items
  add column if not exists export_count integer not null default 0;
