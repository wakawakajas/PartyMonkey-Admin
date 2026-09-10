-- ============================================================
-- A DESIGN'S OWN QUARTER TURN, SHARED WITH THE PRINT TEAM
-- Run in the Supabase SQL Editor AFTER supabase-migration-DESIGN-BLEED.sql,
-- which is where public.design_bleeds comes from.
-- Safe to run more than once, and safe to run as one batch — the Editor wraps
-- a pasted file in a transaction, so nothing here may raise on a second run.
--
-- Artwork does not always arrive standing the way it prints. A card drawn
-- landscape that goes into a portrait box is turned a quarter before it is
-- laid down, and that turn belongs to the design rather than to the person who
-- noticed: whoever lays that file on a sheet next month should lay it the way
-- round it was proved, on their machine, without being told. It is kept beside
-- the bleed because it is the same kind of fact about the same file, and one
-- row per design is one thing to correct rather than two.
-- ============================================================

-- ---------- PART 1 of 3 : the turn ----------
-- Quarters only. Anything else leaves a rectangle that no longer fits the box
-- it is printed in, and 0/90/180/270 are the only turns a design ever needs.
-- Zero is the design standing the way its file has it, which is why the app
-- deletes the row rather than writing a zero when a turn is taken back off.
alter table public.design_bleeds
  add column if not exists rot smallint not null default 0;

do $$
begin
  alter table public.design_bleeds
    add constraint design_bleeds_rot_quarter check (rot in (0, 90, 180, 270));
exception
  when duplicate_object then null;   -- already there: a second run, not a problem
end $$;


-- ---------- PART 2 of 3 : a row may now carry a turn and no bleed ----------
-- The row used to exist only while a design kept its own bleed. It now also
-- exists for a design that keeps only its own turn, so mm has to be allowed to
-- say nothing — and saying nothing is not the same as saying zero: null is
-- "this design bleeds by whatever the sheet says", 0 is "this design does not
-- bleed at all". The check that mm is not negative stays exactly as it was;
-- it passes for null on its own.
alter table public.design_bleeds
  alter column mm drop not null;


-- ---------- PART 3 of 3 : check ----------
-- The column, its default and whether it may be null:
--   select column_name, data_type, is_nullable, column_default
--     from information_schema.columns
--    where table_schema='public' and table_name='design_bleeds'
--    order by ordinal_position;
--
-- And what is currently laid down other than the way its file has it:
--   select path, mm, rot, updated_at from public.design_bleeds
--    where rot <> 0 or mm is not null order by path;
