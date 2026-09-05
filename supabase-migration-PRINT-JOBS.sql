-- ============================================================
-- PRINT JOBS — a sheet that is saved and come back to
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql
-- (which is where public.on_team comes from). Safe to run more than once.
--
-- A sheet used to have one way off the builder: download it. That is right
-- for a Custom Print Template, which is one order's own card and is printed
-- the moment it is made. It is wrong for everything that shares a sheet — a
-- gift tag page, the batch's print sheet — because those are rarely full.
-- Six tags this morning and eleven tomorrow is one sheet through the machine,
-- not two half-empty ones, and the only way to hold the first six until the
-- eleven arrived was to leave a builder open on somebody's screen overnight.
--
-- A job is that sheet, written down. It holds what is on it — the designs,
-- the words, the quantities — and nothing that cannot be written down: the
-- artwork is named by its path in the designs folder, never by its pixels,
-- exactly the way a saved template names it. Open it, add this morning's
-- lines to it, save it again; download it the day it is full.
-- ============================================================

-- ---------- PART 1 of 3 : the table ----------
-- kind   'print'    the print sheet, laid on a saved template
--        'giftTag'  a gift tag page's sheet
-- page   which gift tag page ('festive' / 'custom'). Empty for a print sheet.
-- data   everything the builder needs to lay this sheet out again. For a
--        print job that is the same shape a saved template's data has; for a
--        gift tag job it is the order lines, the page and the finish.
-- items  how many cards or tags are on it, kept alongside so the list can say
--        so without every job's data being read to find out.
create table if not exists public.print_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null default '',
  kind text not null check (kind in ('print','giftTag')),
  page text not null default '',
  data jsonb not null default '{}'::jsonb,
  items integer not null default 0,
  -- a job that has been printed is finished with, but not deleted: it is the
  -- record of what went through the machine
  status text not null default 'open' check (status in ('open','done')),
  note text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists print_jobs_open_idx
  on public.print_jobs(status, updated_at desc);

alter table public.print_jobs enable row level security;


-- ---------- PART 2 of 3 : shared with the team ----------
-- The same decision as the batches and the routing: a sheet held open for
-- tomorrow is held open for whoever is at the machine tomorrow, which is not
-- necessarily the person who started it.
drop policy if exists "team_select" on public.print_jobs;
drop policy if exists "team_insert" on public.print_jobs;
drop policy if exists "team_update" on public.print_jobs;
drop policy if exists "team_delete" on public.print_jobs;

create policy "team_select" on public.print_jobs
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.print_jobs
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.print_jobs
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.print_jobs
  for delete using (public.on_team(auth.uid()));


-- ---------- PART 3 of 3 : live updates between devices ----------
-- Two people adding to the same held-open sheet from two machines must be
-- adding to the same sheet, not to two copies of it that overwrite each
-- other. The list refreshes itself when anybody writes.
alter table public.print_jobs replica identity full;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'print_jobs')
  then
    alter publication supabase_realtime add table public.print_jobs;
  end if;
end $$;
