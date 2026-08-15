-- ============================================================
-- REQUEST & REMINDER, PART THREE: send to several people, and a note back
-- Run in the Supabase SQL Editor AFTER supabase-migration-REQUESTS.sql and
-- supabase-migration-REQUESTS2.sql. Safe to run more than once, and safe to
-- run as one batch — the Editor wraps a pasted file in a transaction, so
-- nothing here may raise on a second run or it would undo everything above.
--
-- Sending to several people still writes one row per person — that is what
-- lets each of them see it, acknowledge it and be reminded about it on their
-- own. group_id is the only new idea: it says which rows were the same ask,
-- so that the first person to take it can close it for everyone else.
-- ============================================================

-- ---------- PART 1 of 3 : the group, and a note back ----------
alter table public.requests
  add column if not exists group_id uuid;
alter table public.requests
  add column if not exists remarks text not null default '';

-- only asked when closing siblings on an acknowledge, so only rows that are
-- actually part of a group need to be found quickly
create index if not exists requests_group_idx on public.requests(group_id)
  where group_id is not null;

-- 'claimed' is a fourth status: not pending, not this person's to act on
-- either, because somebody else on the same ask got there first. Whatever
-- named the original check constraint, this finds it and replaces it rather
-- than assuming the default name — safe however this table was created.
do $$
declare
  con text;
begin
  select conname into con
    from pg_constraint
   where conrelid = 'public.requests'::regclass
     and contype = 'c'
     and pg_get_constraintdef(oid) ilike '%status%pending%';
  if con is not null then
    execute format('alter table public.requests drop constraint %I', con);
  end if;
end $$;

alter table public.requests
  add constraint requests_status_check
  check (status in ('pending','acknowledged','completed','claimed'));


-- ---------- PART 2 of 3 : first to answer closes it for the rest ----------
-- Fires after a row moves off pending onto acknowledged or completed. Every
-- other row sharing its group_id that is still pending stops being anyone
-- else's to do: it goes to 'claimed', and requests_clear_remind (from part
-- one) has already taught the app that only a null remind_at is quiet, so
-- clearing it here is what stops that sibling ringing again.
--
-- security definer, owned the same way team_members() and
-- purge_old_requests() are, because this has to reach across into rows whose
-- assignee is somebody else entirely — that is the whole point of it, and
-- the row policies rightly do not allow it otherwise.
create or replace function public.requests_close_siblings()
returns trigger language plpgsql
security definer
set search_path = public
as $func$
begin
  if new.group_id is not null
     and old.status = 'pending'
     and new.status in ('acknowledged','completed') then
    update public.requests
       set status = 'claimed', remind_at = null
     where group_id = new.group_id
       and id <> new.id
       and status = 'pending';
  end if;
  return new;
end $func$;

drop trigger if exists requests_close_siblings_trg on public.requests;
create trigger requests_close_siblings_trg
  after update on public.requests
  for each row execute function public.requests_close_siblings();


-- ---------- PART 3 of 3 : nothing else changes ----------
-- Row level security, the read/insert/update/delete policies, realtime and
-- the reminder sweep all already work per-row and need nothing added: a
-- group is only ever the plain requests table with a shared id sitting on a
-- few of its rows.
