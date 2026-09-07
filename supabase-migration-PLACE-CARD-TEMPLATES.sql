-- ============================================================
-- PLACE CARD TEMPLATES — one more kind of saved template
-- Run in the Supabase SQL Editor AFTER supabase-migration-SHARED-TEMPLATES.sql.
-- Safe to run more than once, and safe to run as one batch.
--
-- The Place Card screen is the Custom Template builder wearing a second hat: it
-- lays text straight on the sheet and keeps its own list of saved layouts. That
-- list lives in the same public.templates table as the print templates, told
-- apart by a new `kind` column:
--
--   kind = 'print'       a Custom Print Template layout          (the default)
--   kind = 'place-card'  a Place Card layout, text objects and all
--
-- Nothing about who may read or delete a row changes: the print_team_* policies
-- from supabase-migration-SHARED-TEMPLATES.sql are kind-agnostic, so a Place
-- Card template is shared with everyone who has Custom Print Template, exactly
-- like a print template already is.
--
-- IMPORTANT: deploy the app change that goes with this. The app that ships with
-- it saves with an explicit kind and addresses the unique key as
-- (user_id, name, kind); an older build would break its own upsert once the key
-- moves.
-- ============================================================

-- ---------- PART 1 of 3 : the column ----------
alter table public.templates
  add column if not exists kind text not null default 'print';

do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'templates_kind_chk') then
    alter table public.templates
      add constraint templates_kind_chk check (kind in ('print','place-card'));
  end if;
end $$;


-- ---------- PART 2 of 3 : the unique key ----------
-- A name is unique per person per kind now, not per person. One operator may
-- have a print "Wedding" and a place-card "Wedding" and they are two rows.
-- Saving still overwrites your own row of that name and kind, never anybody
-- else's, and never the other kind.
alter table public.templates drop constraint if exists templates_user_id_name_key;
drop index if exists templates_user_id_name_key;
create unique index if not exists templates_user_name_kind_key
  on public.templates (user_id, name, kind);


-- ---------- PART 3 of 3 : check ----------
--   select kind, count(*) from public.templates group by kind;
--
-- And that the shared-read policy still stands (four print_team_* rows):
--   select policyname from pg_policies
--    where schemaname='public' and tablename='templates' order by policyname;
