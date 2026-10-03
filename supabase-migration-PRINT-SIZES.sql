-- ============================================================
-- FINANCE — PRINT CALCULATOR: HOW MANY FIT PER SHEET, BY SIZE
-- Run AFTER supabase-migration-FINANCE-COSTS.sql. Safe to run more than once.
--
-- The Print Calculator starts each size with a built-in count per SRA3 sheet
-- (A7 = 16, Namecard = 20 ...). A count saved here replaces it on every PC:
-- {"A7": 18, "Namecard": 21}
-- ============================================================

alter table public.finance_settings add column if not exists print_sizes jsonb not null default '{}'::jsonb;
