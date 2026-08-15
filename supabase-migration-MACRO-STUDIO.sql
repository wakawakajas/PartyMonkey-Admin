-- ============================================================
-- MACRO STUDIO
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql.
-- Safe to run more than once.
--
-- Gates the "Macro Studio" home tile. That tile only opens
-- http://127.0.0.1:8756 in a new tab -- the local desktop automation
-- tool running on whoever's own PC. Nothing here talks to it or stores
-- anything about it; this is only the interface gate, same as every
-- other section.
-- ============================================================

-- defaults to false, so nobody gains it without being given it
alter table public.profiles
  add column if not exists can_macro_studio boolean not null default false;

-- Who can open it — grant it from Users & access, or here:
--   update public.profiles set can_macro_studio = true where email = 'someone@example.com';
