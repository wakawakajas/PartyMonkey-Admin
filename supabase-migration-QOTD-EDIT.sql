-- ============================================================
-- EDITING WHAT WAS WRITTEN
-- Run in the Supabase SQL Editor AFTER supabase-migration-ENERGY-GEMINI.sql.
-- Safe to run more than once.
--
-- The day's question arrives written by a model, and a model occasionally
-- writes something that does not quite land — a joke that misses, a fourth
-- answer nobody would ever pick. An admin can now fix it in place rather than
-- delete the day and hope the next attempt is better.
--
-- Only an admin, and only ever the wording: the day stays its own key, so an
-- edit cannot move a question onto a different day or split one day's answers
-- across two questions.
-- ============================================================

drop policy if exists "energy_generated_insert" on public.energy_generated;
drop policy if exists "energy_generated_update" on public.energy_generated;
drop policy if exists "energy_generated_delete" on public.energy_generated;

-- Days can also be written ahead. The table is keyed by the day, so putting a
-- row there for tomorrow is all it takes: the function finds one already
-- stored and leaves it alone, and the card asks it when tomorrow comes.
create policy "energy_generated_insert" on public.energy_generated
  for insert with check (public.is_app_admin(auth.uid()));

create policy "energy_generated_update" on public.energy_generated
  for update using (public.is_app_admin(auth.uid()));

-- Deleting a day is how you ask for it to be written again: the function finds
-- nothing stored and writes a fresh one. Answers already given are not touched
-- — they live in energy_signals and are keyed by the day, not by the question,
-- which does mean an answer can end up filed against wording nobody saw. That
-- is the price of a second attempt, and it is why editing is offered first.
create policy "energy_generated_delete" on public.energy_generated
  for delete using (public.is_app_admin(auth.uid()));


-- ---------- check ----------
-- The last fortnight, and whether each still has its four answers:
--   select day, question, jsonb_array_length(modes) as slots
--     from public.energy_generated order by day desc limit 14;
