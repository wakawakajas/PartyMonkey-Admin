-- ============================================================
-- FIERY — Magic Create's Download & Print, sent to the press with its settings
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql
-- (which is where public.on_team comes from). Safe to run more than once.
--
-- A Magic Create PDF used to go into the Working Folder and then be dragged
-- into Command WorkStation, where a server preset, the copies and the pages
-- were set by hand. The app cannot do that last part itself: it is served
-- over https, and a browser will not let an https page talk to the Fiery on
-- the LAN. Macro Studio on the shop PC can.
--
-- So the app still saves the file into the Working Folder exactly as before,
-- and then writes down here what to do with it. Macro Studio sees the row,
-- reads that file out of the same folder, and hands it to the Fiery API with
-- the preset, copies and pages -- then either prints it or leaves it held.
-- The file itself never comes through here; only its name does.
--
-- The presets are read the other way: Macro Studio lists them off the Fiery
-- every few minutes and keeps fiery_presets in step, so the pop-up offers
-- exactly the presets Command WorkStation shows, under their own names.
-- ============================================================

-- ---------- PART 1 of 4 : the jobs ----------
-- file_name  the PDF's name in the Working Folder, as the app wrote it
-- folder     the folder the app wrote it into, for the error message only --
--            the shop PC reads from the folder named in fiery.json
-- preset_id  the Fiery's own id for the server preset; preset_name is what it
--            was called when it was picked, for showing
-- copies     Fiery's number of copies
-- pages      blank for all of them, or a range like 1-3,5
-- action     print -> printed straight away; hold -> left in the Held list
-- status     queued  -> waiting for the shop PC
--            sending -> claimed, on its way to the Fiery
--            sent    -> the Fiery has it (fiery_job_id says which job)
--            failed  -> it did not get there, and error says why
create table if not exists public.fiery_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  file_name text not null,
  folder text not null default '',
  preset_id text not null default '',
  preset_name text not null default '',
  copies integer not null default 1 check (copies between 1 and 9999),
  pages text not null default '',
  action text not null default 'hold' check (action in ('print','hold')),
  status text not null default 'queued'
    check (status in ('queued','sending','sent','failed')),
  fiery_job_id text not null default '',
  device text not null default '',
  error text not null default '',
  created_at timestamptz not null default now(),
  claimed_at timestamptz,
  sent_at timestamptz
);

create index if not exists fiery_jobs_queued_idx
  on public.fiery_jobs(status, created_at);


-- ---------- PART 2 of 4 : the presets, as the Fiery has them ----------
-- Written by Macro Studio only. A preset deleted in Command WorkStation is
-- deleted here on the next read, so nothing offers a preset that is gone.
create table if not exists public.fiery_presets (
  id text primary key,
  name text not null,
  synced_at timestamptz not null default now()
);


-- ---------- PART 3 of 4 : shared with the team ----------
-- Whoever is at the batch presses print, and whichever PC runs Macro Studio
-- sends it. None of this is anybody's private row.
alter table public.fiery_jobs enable row level security;
alter table public.fiery_presets enable row level security;

drop policy if exists "team_select" on public.fiery_jobs;
drop policy if exists "team_insert" on public.fiery_jobs;
drop policy if exists "team_update" on public.fiery_jobs;
drop policy if exists "team_delete" on public.fiery_jobs;
create policy "team_select" on public.fiery_jobs
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.fiery_jobs
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.fiery_jobs
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.fiery_jobs
  for delete using (public.on_team(auth.uid()));

drop policy if exists "team_select" on public.fiery_presets;
drop policy if exists "team_insert" on public.fiery_presets;
drop policy if exists "team_update" on public.fiery_presets;
drop policy if exists "team_delete" on public.fiery_presets;
create policy "team_select" on public.fiery_presets
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.fiery_presets
  for insert with check (public.on_team(auth.uid()));
create policy "team_update" on public.fiery_presets
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.fiery_presets
  for delete using (public.on_team(auth.uid()));


-- ---------- PART 4 of 4 : not piling up ----------
-- A sent job is a receipt. Two days, the same as the seed labels. Called by
-- the app whenever a job is sent, so it is nobody's job to remember.
create or replace function public.purge_old_fiery_jobs()
returns void
language sql
security definer
set search_path = public
as $$
  delete from public.fiery_jobs
   where status in ('sent','failed')
     and created_at < now() - interval '2 days';
$$;

grant execute on function public.purge_old_fiery_jobs() to authenticated;

-- ---------- check ----------
--   select name, synced_at from public.fiery_presets order by name;
--   select file_name, preset_name, copies, pages, action, status, error
--     from public.fiery_jobs order by created_at desc limit 20;
