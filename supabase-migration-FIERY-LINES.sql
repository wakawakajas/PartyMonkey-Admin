-- ============================================================
-- FIERY — which batch line a print job is for
-- Run in the Supabase SQL Editor AFTER supabase-migration-FIERY-COPY.sql.
-- Safe to run more than once.
--
-- A copied file sent from a batch is one line of one order. With the line
-- written on the job, the batch can say on that line where its print has got
-- to — waiting, sending, printed, held, or not printed and why — rather than
-- only in a message that has scrolled away.
-- ============================================================

alter table public.fiery_jobs add column if not exists item_id uuid;

create index if not exists fiery_jobs_item_idx
  on public.fiery_jobs(item_id, created_at);
