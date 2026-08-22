-- ============================================================
-- REQUEST & REMINDER, PART FOUR: an admin sees all of them
-- Run in the Supabase SQL Editor AFTER supabase-migration-REQUESTS.sql and
-- supabase-migration-REQUESTS2.sql. Safe to run more than once, and safe to
-- run as one batch — the Editor wraps a pasted file in a transaction, so
-- nothing here may raise on a second run or it would undo everything above.
--
-- A request is addressed to somebody, and the rules so far said only the two
-- people on it may read it. That is still right for everybody else; an admin
-- is the exception, because somebody has to be able to answer "what is
-- outstanding, and who is sitting on it" without asking each person in turn.
--
-- Reading only. Nothing here lets an admin acknowledge, snooze or complete
-- another person's request — those stay with the two people involved, so the
-- record of who actually did it cannot be muddled by somebody watching.
-- (Withdrawing was already an admin's to do, from part one.)
-- ============================================================

-- ---------- PART 1 of 3 : the requests ----------
drop policy if exists "requests_read" on public.requests;
create policy "requests_read" on public.requests
  for select using (
    auth.uid() = requester_id
    or auth.uid() = assignee_id
    or public.is_admin(auth.uid()));


-- ---------- PART 2 of 3 : the photos on them ----------
-- A request without its pictures is half the story — the picture is often the
-- whole of what was asked. Reading only, again: the delete rule is untouched,
-- so an admin still cannot take back somebody else's photo.
drop policy if exists "request_photos_read" on public.request_photos;
create policy "request_photos_read" on public.request_photos
  for select using (
    public.is_admin(auth.uid())
    or exists (
      select 1 from public.requests r
       where r.id = request_id
         and (auth.uid() = r.requester_id or auth.uid() = r.assignee_id)));


-- ---------- PART 3 of 3 : the files behind the photos ----------
-- The row above only hands back a path. This is what lets that path actually
-- be signed and shown. on_this_request is shared by the select and delete
-- storage policies from part two, which is deliberate: the same widening that
-- lets an admin see a finished request's photos also lets them clear the
-- files up, which is the only reason anyone would want to reach them.
create or replace function public.on_this_request(p text)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select public.is_admin(auth.uid()) or exists (
    select 1 from public.requests r
     where 'request-' || r.id::text = (storage.foldername(p))[2]
       and (auth.uid() = r.requester_id or auth.uid() = r.assignee_id)
  );
$func$;
