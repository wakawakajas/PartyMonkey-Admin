-- ============================================================
-- A POP-UP ADDRESSED TO PARTICULAR PEOPLE
-- Run in the Supabase SQL Editor AFTER supabase-migration-ANNOUNCE.sql.
-- Safe to run more than once, and safe to run as one batch.
--
-- Until now every pop-up went to the whole team. It can now be addressed, and
-- an addressed one is not merely hidden from everyone else — the row itself is
-- unreadable to them. Getting this wrong in the app would only hide a message;
-- getting it right here means it was never sent to them.
-- ============================================================

-- ---------- PART 1 of 3 : who it is for ----------
-- NULL, and the empty array, both mean everyone. Two spellings of the same
-- thing is not ideal, but the column has to arrive NULL on the rows already
-- there — and those rows did go to everyone, so NULL already means it.
alter table public.announcements
  add column if not exists recipients uuid[];


-- ---------- PART 2 of 3 : and nobody else ----------
-- Replaces the old "anyone on the team may read any of them". The author is
-- included so a sender can still see what they sent; everybody else has to be
-- named on it.
drop policy if exists "team_read" on public.announcements;

create policy "team_read" on public.announcements
  for select using (
    public.on_team(auth.uid())
    and (
      recipients is null
      or cardinality(recipients) = 0
      or auth.uid() = any (recipients)
      or auth.uid() = user_id
    )
  );

-- Realtime checks the same policy per subscriber, so an addressed message is
-- not pushed down the socket to people it is not for. The app checks again on
-- arrival regardless — a filter you cannot see the workings of is not one to
-- show somebody else's message on the strength of.


-- ---------- PART 3 of 3 : check ----------
-- What has been sent, and to whom. NULL in the last column is "everyone":
--   select created_at, title,
--          coalesce(cardinality(recipients)::text, 'everyone') as sent_to
--     from public.announcements order by created_at desc limit 20;
--
-- To satisfy yourself the policy bites, as a non-admin account:
--   select count(*) from public.announcements;
-- should not count messages addressed to other people.
