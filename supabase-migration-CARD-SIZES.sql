-- ============================================================
-- FINANCE — CARD SIZES: HOW MANY PIECES FIT ONE SHEET
-- Run AFTER supabase-migration-FINANCE-COSTS.sql. Safe to run more than once.
--
-- Edited in Finance settings (add / rename / change the count / delete) and
-- picked from in the Print Calculator. Replaces the older print_sizes
-- overrides, which are no longer read.
-- [{"name": "A4", "ups": 2}, ...]
-- ============================================================

alter table public.finance_settings add column if not exists card_sizes jsonb not null default
  '[{"name":"A3","ups":1},{"name":"A4","ups":2},{"name":"A5","ups":5},{"name":"A6","ups":6},{"name":"A7","ups":12},
    {"name":"A4 Folded","ups":1},{"name":"A5 Folded","ups":2},{"name":"A6 Folded","ups":4},
    {"name":"Namecard","ups":20},{"name":"Placecard","ups":12}]'::jsonb;
