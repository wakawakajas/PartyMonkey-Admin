-- ============================================================
-- NEW SECTIONS IN USERS & ACCESS + ADMIN POP-UP MESSAGES
-- Run in the Supabase SQL Editor, AFTER supabase-migration-ACCESS.sql.
-- Safe to run more than once. Plain statements, no DO blocks.
-- ============================================================

-- ---------- PART 1 of 3 : the two new sections ----------
-- they default to true so nobody loses what they can already reach today
alter table public.profiles add column if not exists can_adhoc     boolean not null default true;
alter table public.profiles add column if not exists can_warehouse boolean not null default true;


-- ---------- PART 2 of 3 : messages an admin pushes to everyone ----------
create table if not exists public.announcements (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text not null default '',
  body  text not null default '',
  created_at timestamptz not null default now()
);

alter table public.announcements enable row level security;

drop policy if exists "team_read"    on public.announcements;
drop policy if exists "admin_write"  on public.announcements;
drop policy if exists "admin_delete" on public.announcements;

-- everyone with an account sees them; only an admin writes them
create policy "team_read" on public.announcements
  for select using (public.on_team(auth.uid()));
create policy "admin_write" on public.announcements
  for insert with check (public.is_admin(auth.uid()) and auth.uid() = user_id);
create policy "admin_delete" on public.announcements
  for delete using (public.is_admin(auth.uid()));

-- so a message reaches an app that is already open
alter table public.announcements replica identity full;


-- ---------- PART 3 of 3 : deliver them live ----------
-- ignore an error here; it only means realtime is already carrying the table
alter publication supabase_realtime add table public.announcements;
