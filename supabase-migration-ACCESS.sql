-- ============================================================
-- ACCESS CONTROL — run this ONE file in the Supabase SQL Editor.
-- Safe to run more than once.
--
--   1. Dashboard -> SQL Editor -> New query
--   2. Click in the box, Ctrl+A then Delete (make sure it is EMPTY)
--   3. Paste this whole file, click Run
--   4. Expect: "Success. No rows returned"
-- ============================================================

-- one row per person, saying which parts of the app they may open
create table if not exists public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  email text,
  is_admin     boolean not null default false,
  can_print    boolean not null default false,
  can_shipping boolean not null default false,
  can_pickup   boolean not null default false,
  created_at timestamptz not null default now()
);

alter table public.profiles enable row level security;

-- A policy on profiles that reads profiles would recurse forever. security
-- definer runs as the function's owner, skipping row level security, which
-- breaks the loop.
create or replace function public.is_admin(uid uuid)
returns boolean language sql security definer stable set search_path = public as $$
  select coalesce((select is_admin from public.profiles where user_id = uid), false);
$$;

do $$
begin
  execute 'drop policy if exists "read_own_or_admin" on public.profiles';
  execute 'drop policy if exists "admin_writes"      on public.profiles';
  execute 'drop policy if exists "admin_inserts"     on public.profiles';
  -- you can always read your own row; an admin reads everyone's
  execute 'create policy "read_own_or_admin" on public.profiles for select
             using (auth.uid() = user_id or public.is_admin(auth.uid()))';
  -- only an admin may change who sees what
  execute 'create policy "admin_writes" on public.profiles for update
             using (public.is_admin(auth.uid()))';
  execute 'create policy "admin_inserts" on public.profiles for insert
             with check (public.is_admin(auth.uid()))';
end $$;

-- every new account gets a row automatically, with nothing switched on
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles(user_id, email) values (new.id, new.email)
  on conflict (user_id) do nothing;
  return new;
end $$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- give the accounts that already exist a row
insert into public.profiles(user_id, email)
select id, email from auth.users
on conflict (user_id) do nothing;

-- keep the email column readable as accounts change theirs
update public.profiles p set email = u.email
from auth.users u where u.id = p.user_id and p.email is distinct from u.email;

-- ---- make the owner an admin with the run of the app -------------------
-- everyone else starts with nothing switched on, and you grant it from the
-- Users screen. Change the address here if you sign in with a different one.
update public.profiles
   set is_admin = true, can_print = true, can_shipping = true, can_pickup = true
 where email = 'jasminewaka21@gmail.com';
