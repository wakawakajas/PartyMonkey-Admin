-- ============================================================
-- SEED LABELS — a label pressed on the tablet, printed on the P-touch
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql
-- (which is where public.on_team comes from). Safe to run more than once.
--
-- The bundle is packed on a tablet. The P-touch is plugged into the PC in
-- the packing area. Those two cannot talk: a tablet has no driver for that
-- printer, and the app is served over https, which a browser will not let
-- reach a plain-http box on the same LAN. The one thing both of them can
-- reach is this project.
--
-- So a label is written down here. Pressing print in Bundle SKU inserts a
-- row; Macro Studio on the packing PC is signed in to the same project,
-- picks it up within a few seconds and sends it to the tape. Nothing is
-- confirmed at either end — the row going from queued to printed is the
-- confirmation, and the tablet watches for that rather than asking.
--
-- What is on the label is decided on the tablet and stored here as the words
-- themselves, not as the SKU it came from: the seed's name with the word
-- Seed or Seeds taken out. The sub-SKU it came from rides along beside it so
-- a label that came out wrong can be traced back to the line that asked for
-- it, but nothing re-derives the words at print time. What you saw on the
-- button is what comes off the machine.
-- ============================================================

-- ---------- PART 1 of 3 : the table ----------
-- text     exactly what is printed, already cleaned up
-- sku      the sub-SKU it came from, for tracing only
-- copies   how many of this same label to run off
-- status   queued  -> waiting for the packing PC
--          printing-> claimed by a PC, on its way to the tape
--          printed -> out of the machine
--          failed  -> it did not print, and error says why
-- device   which PC took it, so two benches can be told apart
create table if not exists public.label_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  store text not null default '',
  -- which bundle job asked for it. Set null rather than cascade: an order
  -- deleted in the evening must not delete the record of what was printed.
  job_id uuid references public.bundle_jobs(id) on delete set null,
  sku text not null default '',
  text text not null,
  copies integer not null default 1 check (copies between 1 and 99),
  status text not null default 'queued'
    check (status in ('queued','printing','printed','failed')),
  device text not null default '',
  error text not null default '',
  created_at timestamptz not null default now(),
  claimed_at timestamptz,
  printed_at timestamptz
);

-- the agent's one query: what is queued, oldest first
create index if not exists label_jobs_queued_idx
  on public.label_jobs(status, created_at);
-- the tablet's one query: what this order has printed
create index if not exists label_jobs_job_idx
  on public.label_jobs(job_id, created_at desc);


-- ---------- PART 2 of 3 : shared with the team ----------
-- The same decision as the bundles themselves: whoever is at the bench
-- presses print, and whichever PC is switched on prints it. A label queued
-- by one person and printed by another machine is the normal case, not an
-- exception, so none of this is anybody's private row.
alter table public.label_jobs enable row level security;

drop policy if exists "team_select" on public.label_jobs;
drop policy if exists "team_insert" on public.label_jobs;
drop policy if exists "team_update" on public.label_jobs;
drop policy if exists "team_delete" on public.label_jobs;

create policy "team_select" on public.label_jobs
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.label_jobs
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.label_jobs
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.label_jobs
  for delete using (public.on_team(auth.uid()));


-- ---------- PART 3 of 3 : live updates, and not piling up ----------
-- The tablet has to see queued become printed without asking, or pressing
-- print is a button that does nothing visible for three seconds and then
-- still nothing.
alter table public.label_jobs replica identity full;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'label_jobs')
  then
    alter publication supabase_realtime add table public.label_jobs;
  end if;
end $$;

-- A printed label is a receipt, not an archive. Two days is long enough to
-- ask why a tape came out wrong and short enough that the table stays small;
-- the same two days the packed bundles get. Called by the app whenever the
-- Bundle SKU screen is opened, so it is nobody's job to remember.
create or replace function public.purge_old_label_jobs()
returns void
language sql
security definer
set search_path = public
as $$
  delete from public.label_jobs
   where status in ('printed','failed')
     and created_at < now() - interval '2 days';
$$;

grant execute on function public.purge_old_label_jobs() to authenticated;
