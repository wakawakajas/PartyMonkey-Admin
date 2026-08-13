-- ============================================================
-- REQUEST & REMINDER
-- Run in the Supabase SQL Editor, AFTER supabase-migration-ACCESS.sql
-- and supabase-migration-ADHOC.sql (which is where public.on_team comes
-- from). Safe to run more than once. Plain statements, no DO blocks.
--
-- One person asks another for something. It keeps asking — on a snooze the
-- reminder comes back, and comes back again, until the assignee says it is
-- Completed. Both sides can see it; nobody else can.
-- ============================================================

-- ---------- PART 1 of 5 : who you can ask ----------
-- The profiles table only lets you read your own row unless you are an admin,
-- which is right for access control but leaves an ordinary account with no way
-- to name a colleague. This hands back the directory and nothing else: an id
-- and an address per person, no permission flags, and only to someone who is
-- themselves on the team.
create or replace function public.team_members()
returns table(user_id uuid, email text)
language sql
security definer
stable
set search_path = public
as $func$
  select p.user_id, p.email
    from public.profiles p
   where public.on_team(auth.uid())
   order by p.email;
$func$;

revoke all on function public.team_members() from public;
grant execute on function public.team_members() to authenticated;


-- ---------- PART 2 of 5 : the requests ----------
create table if not exists public.requests (
  id uuid primary key default gen_random_uuid(),
  requester_id uuid not null references auth.users(id) on delete cascade,
  assignee_id  uuid not null references auth.users(id) on delete cascade,
  title text not null,
  body  text not null default '',
  -- pending -> acknowledged -> completed. Acknowledged means "I have seen it",
  -- not "it is done": the reminders carry on until completed, which is the
  -- only status that stops them.
  status text not null default 'pending'
    check (status in ('pending','acknowledged','completed')),
  acknowledged_at timestamptz,
  completed_at    timestamptz,
  -- When the next reminder is due. NULL means it is due now — either it has
  -- just been sent and is waiting to be dealt with, or a snooze has run out
  -- and the alert is back on screen. Clearing it after each ring is what makes
  -- the sweep in PART 5 safe to run every minute without double-buzzing.
  remind_at timestamptz,
  snoozes integer not null default 0,
  created_at timestamptz not null default now()
);

-- the two questions this table is ever asked: what is on my plate, and what
-- did I ask for
create index if not exists requests_assignee_idx  on public.requests(assignee_id, status);
create index if not exists requests_requester_idx on public.requests(requester_id, status);
-- and the one the reminder sweep asks
create index if not exists requests_due_idx on public.requests(remind_at)
  where remind_at is not null;

alter table public.requests enable row level security;

drop policy if exists "requests_read"   on public.requests;
drop policy if exists "requests_insert" on public.requests;
drop policy if exists "requests_update" on public.requests;
drop policy if exists "requests_delete" on public.requests;

-- Only the two people involved, which is narrower than the rest of the app on
-- purpose — a request is addressed to somebody, not posted to the floor.
create policy "requests_read" on public.requests
  for select using (auth.uid() = requester_id or auth.uid() = assignee_id);
-- you may only send as yourself, and only to somebody who has an account
create policy "requests_insert" on public.requests
  for insert with check (
    auth.uid() = requester_id
    and public.on_team(auth.uid())
    and public.on_team(assignee_id));
-- the assignee acknowledges, snoozes and completes; the requester can edit or
-- close their own ask
create policy "requests_update" on public.requests
  for update using (auth.uid() = requester_id or auth.uid() = assignee_id);
-- but only the person who asked can withdraw it
create policy "requests_delete" on public.requests
  for delete using (auth.uid() = requester_id or public.is_admin(auth.uid()));


-- ---------- PART 3 of 5 : deliver them live ----------
-- so a request lands on a screen that is already open, without a refresh
alter table public.requests replica identity full;

-- ignore an error here; it only means realtime is already carrying the table
alter publication supabase_realtime add table public.requests;


-- ---------- PART 4 of 5 : housekeeping ----------
-- A completed request has nothing left to ring about, so it never wants a
-- reminder time. Doing it here rather than in the app means it also holds for
-- a row closed from somewhere else.
create or replace function public.requests_clear_remind()
returns trigger language plpgsql set search_path = public as $func$
begin
  if new.status = 'completed' then
    new.remind_at := null;
    if new.completed_at is null then new.completed_at := now(); end if;
  end if;
  return new;
end $func$;

drop trigger if exists requests_clear_remind_trg on public.requests;
create trigger requests_clear_remind_trg
  before insert or update on public.requests
  for each row execute function public.requests_clear_remind();


-- ============================================================
-- PART 5 of 5 : reminders while the app is CLOSED  — OPTIONAL
-- ============================================================
-- Everything above already works with the app open: the snooze timer runs in
-- the page and the alert comes back on its own. This part is what makes a
-- snooze survive the phone being locked and the app being shut, by having the
-- database itself call the notify-request function once a minute.
--
-- Skip it if you do not need that. Nothing else depends on it.
--
-- To switch it on:
--   1. Deploy the notify-request function first.
--   2. Dashboard -> Database -> Extensions: enable pg_cron and pg_net.
--   3. Replace the two placeholders below with your own values:
--        <PROJECT-REF>        Settings -> General -> Reference ID
--        <SERVICE-ROLE-KEY>   Settings -> API -> service_role  (secret!)
--      This key can read and write everything, so keep this SQL to yourself.
--   4. Run this part on its own.
--
-- To switch it off again:
--   select cron.unschedule('pigu-request-reminders');
-- ------------------------------------------------------------
--
-- create extension if not exists pg_cron;
-- create extension if not exists pg_net;
--
-- select cron.schedule(
--   'pigu-request-reminders',
--   '* * * * *',
--   $cron$
--   select net.http_post(
--     url     := 'https://<PROJECT-REF>.supabase.co/functions/v1/notify-request',
--     headers := jsonb_build_object(
--                  'Content-Type',  'application/json',
--                  'Authorization', 'Bearer <SERVICE-ROLE-KEY>'),
--     body    := jsonb_build_object('due', true)
--   );
--   $cron$
-- );
--
-- -- what it has been doing:
-- --   select * from cron.job_run_details order by start_time desc limit 20;
