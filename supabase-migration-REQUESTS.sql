-- ============================================================
-- REQUEST & REMINDER
-- Run in the Supabase SQL Editor, AFTER supabase-migration-ACCESS.sql
-- and supabase-migration-ADHOC.sql (which is where public.on_team comes
-- from). Safe to run more than once, and safe to run as one batch — the
-- Editor wraps the file in a transaction, so nothing here may raise on a
-- second run or it would undo everything above it.
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

-- Asked first rather than attempted and forgiven. Adding a table that is
-- already published raises 42710, and the SQL Editor runs this file as one
-- transaction — so that error would roll back everything above it, not just
-- this line. Some projects publish every table automatically, which makes it
-- the normal case rather than the odd one.
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'requests')
  then
    alter publication supabase_realtime add table public.requests;
  end if;
end $$;


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
-- ---- WHAT TO DO ------------------------------------------------------
--
-- STEP 1. Deploy the notify-request function, if you have not already.
--
-- STEP 2. Turn on two database add-ons:
--         Dashboard -> Database -> Extensions -> search "pg_cron"  -> enable
--                                             -> search "pg_net"   -> enable
--
-- STEP 3. Make up a password and tell the function about it:
--         Dashboard -> Edge Functions -> notify-request -> Secrets -> Add
--           Name:  CRON_SECRET
--           Value: any long random string you like
--
--         A password of your own rather than the project's service_role key.
--         The scheduled job below is stored in the database in plain sight,
--         so whatever goes in it should be worth as little as possible: this
--         one can only ask the function to send reminders that were already
--         due. The service_role key sitting in the same place could read and
--         rewrite everything you have.
--
--         (The function still accepts the service_role key, if you would
--         rather use that. It is under Project Settings -> API Keys ->
--         "Legacy API keys". CRON_SECRET is the better answer.)
--
-- STEP 4. Copy the block below into the SQL Editor — everything between the
--         two ==== lines, with the leading "--" removed from each line.
--         Replace PASTE_YOUR_CRON_SECRET_HERE with what you chose in STEP 3,
--         keeping the single quotes around it. Then Run.
--
--         The project address and the long "eyJ..." key are already filled in.
--         Neither is secret: that is the same public anon key the app ships
--         with. It is there only because the platform gateway rejects a call
--         whose Authorization header is not a real token — it does that
--         before the function runs, so a password in that header never
--         arrives. The password goes in x-cron-secret instead, and that is
--         the thing that actually grants access.
--
-- STEP 5. Check it is running:
--         select * from cron.job_run_details order by start_time desc limit 5;
--         A row a minute, with status "succeeded". Nothing appears until the
--         next whole minute ticks over.
--
-- To switch it off again:
--         select cron.unschedule('pigu-request-reminders');
--
-- ==== copy from here ==================================================
--
-- create extension if not exists pg_cron;
-- create extension if not exists pg_net;
--
-- select cron.schedule(
--   'pigu-request-reminders',
--   '* * * * *',
--   $cron$
--   select net.http_post(
--     url     := 'https://kdwggyzyzrhiniasilqs.supabase.co/functions/v1/notify-request',
--     headers := jsonb_build_object(
--                  'Content-Type',   'application/json',
--                  'Authorization',  'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imtkd2dneXp5enJoaW5pYXNpbHFzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODYwNTE3ODEsImV4cCI6MjEwMTYyNzc4MX0.WOJjkSZ0yf0zAe_I9jFJbuH2mNPtzZj_kexq0EX_7f8',
--                  'x-cron-secret',  'PASTE_YOUR_CRON_SECRET_HERE'),
--     body    := jsonb_build_object('due', true)
--   );
--   $cron$
-- );
--
-- ==== to here =========================================================
