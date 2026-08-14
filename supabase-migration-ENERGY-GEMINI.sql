-- ============================================================
-- A WRITTEN-EACH-DAY ENERGY QUESTION
-- Run in the Supabase SQL Editor AFTER supabase-migration-ENERGY.sql.
-- Safe to run more than once, and safe to run as one batch.
--
-- The hand-written sets are drawn from, one a day, and go round. This adds a
-- second source: an admin sets a theme for the week and the question is
-- written to it each morning.
--
-- The generated question is STORED, not asked for per person. Everyone must be
-- answering the same question or the mix means nothing — and a model asked the
-- same thing twice does not give the same answer twice. So the first device to
-- open the app on a given day causes it to be written, and every device after
-- that reads what was written.
--
-- Needs the edge function `energy-question` deployed, and a GEMINI_API_KEY
-- secret set on it. Until then nothing changes: the switch stays off and the
-- hand-written sets carry on exactly as before.
-- ============================================================

-- ---------- PART 1 of 4 : the theme ----------
-- One row, ever, like the calculator's rates. The id is fixed so there is
-- nothing to look up and no way to end up with two themes disagreeing.
create table if not exists public.energy_theme (
  id boolean primary key default true check (id),
  -- what this week is about, in the admin's own words: "the run-up to
  -- Christmas", "coming back from the shutdown", "the new warehouse"
  theme text not null default '',
  -- off until somebody turns it on, so running this migration changes nothing
  enabled boolean not null default false,
  updated_at timestamptz not null default now(),
  updated_by uuid references auth.users(id)
);

insert into public.energy_theme(id) values (true) on conflict (id) do nothing;

alter table public.energy_theme enable row level security;

drop policy if exists "energy_theme_read"  on public.energy_theme;
drop policy if exists "energy_theme_write" on public.energy_theme;

-- everyone reads it, because the card says what today's question was written
-- about; only an admin sets it
create policy "energy_theme_read" on public.energy_theme
  for select using (public.on_team(auth.uid()));
create policy "energy_theme_write" on public.energy_theme
  for update using (public.is_app_admin(auth.uid()));


-- ---------- PART 2 of 4 : what was written, per day ----------
-- modes carries the same promise as energy_sets.modes: exactly four, ordered
-- most capacity first, least last. That ordering is the whole reason a Monday
-- and a Friday can be compared when the wording differs, so it is checked here
-- rather than trusted — a model will cheerfully return three or five.
create table if not exists public.energy_generated (
  day date primary key,
  theme text not null default '',
  question text not null,
  modes jsonb not null,
  created_at timestamptz not null default now()
);

alter table public.energy_generated drop constraint if exists energy_generated_four_modes;
alter table public.energy_generated add constraint energy_generated_four_modes
  check (jsonb_typeof(modes) = 'array' and jsonb_array_length(modes) = 4);

alter table public.energy_generated enable row level security;

drop policy if exists "energy_generated_read" on public.energy_generated;

-- Read by the team, written by nobody. The edge function holds the service
-- role key and so is not subject to this; that is deliberate, and it is what
-- stops one phone writing a different question from everyone else's.
create policy "energy_generated_read" on public.energy_generated
  for select using (public.on_team(auth.uid()));


-- ---------- PART 3 of 4 : deploying the writer ----------
-- The function lives at supabase/functions/energy-question. Deploy it and give
-- it a key:
--
--   supabase functions deploy energy-question
--   supabase secrets set GEMINI_API_KEY=...
--
-- Optionally also GEMINI_MODEL, which defaults to gemini-2.5-flash. It is a
-- secret rather than a constant so the model can be changed without a redeploy
-- when a better or cheaper one turns up.


-- ---------- PART 4 of 4 : check ----------
-- The theme as it stands:
--   select theme, enabled, updated_at from public.energy_theme;
--
-- What has been written so far, newest first:
--   select day, question, jsonb_array_length(modes) as slots, theme
--     from public.energy_generated order by day desc limit 14;
--
-- To make today be written again — after changing the theme mid-week, say —
-- delete the day and reopen the app:
--   delete from public.energy_generated where day = current_date;
