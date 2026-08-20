-- ============================================================
-- DAILY PICK LIST: HOW THE VOICE BEHAVES, PER SHOP
-- Run in the Supabase SQL Editor AFTER supabase-migration-DAILY-PICK-LIST.sql
-- and supabase-migration-ADHOC.sql (for public.on_team).
-- Safe to run more than once.
--
-- One row per shop. Three questions, answered separately for PartyMonkey and
-- Plant Talks because the two floors are picked differently:
--
--   how fast   — rate, 0.5 (slow) to 1.6 (quick)
--   what       — the SKU name, the shelf, the quantity: any, all or none
--   when       — for each thing a picker taps, whether it says nothing, says
--                the line just tapped, or says the one after it
--
-- It lives here rather than on the phone because it is how a shop works
-- rather than a preference of whoever happens to be holding the phone: set
-- it once and everybody picking that shop's sheets gets it.
-- ============================================================

create table if not exists public.pick_list_voice (
  store text primary key check (store in ('partymonkey','planttalks')),

  -- 1 is the voice's own speed. Below that is clearer over machinery,
  -- above it is quicker down a long sheet.
  rate numeric not null default 0.92 check (rate >= 0.5 and rate <= 1.6),

  -- what it reads out, in this order
  say_sku   boolean not null default false,
  say_shelf boolean not null default false,
  say_qty   boolean not null default true,

  -- and when. 'off' says nothing; 'this' reads the line that was tapped;
  -- 'next' reads the one the picker is about to walk to, which is what makes
  -- a sheet workable without looking at the screen.
  on_line    text not null default 'this' check (on_line    in ('off','this','next')),
  on_ok      text not null default 'off'  check (on_ok      in ('off','this','next')),
  on_adhoc   text not null default 'off'  check (on_adhoc   in ('off','this','next')),
  on_request text not null default 'off'  check (on_request in ('off','this','next')),
  -- step-by-step picking has always read the line it moves to; that is the
  -- whole of what it is for, so it starts there
  on_start   text not null default 'this' check (on_start   in ('off','this','next')),

  updated_at timestamptz not null default now()
);

alter table public.pick_list_voice enable row level security;

drop policy if exists "team_select" on public.pick_list_voice;
drop policy if exists "team_insert" on public.pick_list_voice;
drop policy if exists "team_update" on public.pick_list_voice;

-- one setting for the whole floor, the same as the sheets it applies to
create policy "team_select" on public.pick_list_voice
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.pick_list_voice
  for insert with check (public.on_team(auth.uid()));
create policy "team_update" on public.pick_list_voice
  for update using (public.on_team(auth.uid()));

-- so a change made on one phone reaches the others
alter table public.pick_list_voice replica identity full;

-- Both shops start where the app already was: the quantity, read a shade
-- under speed, on step-by-step picking — plus reading a line when it is
-- tapped, which is what this table was added for. Only ever inserted, never
-- overwritten, so running the file again leaves whatever was set alone.
insert into public.pick_list_voice (store) values ('partymonkey'), ('planttalks')
on conflict (store) do nothing;
