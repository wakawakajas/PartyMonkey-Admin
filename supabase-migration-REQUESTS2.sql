-- ============================================================
-- REQUEST & REMINDER, PART TWO: urgency, photos, and clearing up
-- Run in the Supabase SQL Editor AFTER supabase-migration-REQUESTS.sql.
-- Safe to run more than once, and safe to run as one batch — the Editor
-- wraps a pasted file in a transaction, so nothing here may raise on a
-- second run or it would undo everything above it.
-- ============================================================

-- ---------- PART 1 of 4 : urgent or not ----------
-- Defaults to false, so everything already sent stays an ordinary request.
alter table public.requests
  add column if not exists urgent boolean not null default false;


-- ---------- PART 2 of 4 : photos ----------
-- A child table rather than an array column on requests: both people can be
-- adding a picture at the same moment, and two writes to one array means the
-- slower one silently wipes the faster one out.
create table if not exists public.request_photos (
  id uuid primary key default gen_random_uuid(),
  request_id uuid not null references public.requests(id) on delete cascade,
  -- who added it, which is how "three each" is counted and how the delete
  -- rule knows whose it is
  user_id uuid not null references auth.users(id) on delete cascade,
  path text not null,
  created_at timestamptz not null default now()
);

create index if not exists request_photos_request_idx
  on public.request_photos(request_id);

alter table public.request_photos enable row level security;

drop policy if exists "request_photos_read"   on public.request_photos;
drop policy if exists "request_photos_insert" on public.request_photos;
drop policy if exists "request_photos_delete" on public.request_photos;

-- Both people on the request see every picture on it, whoever added it —
-- that is the point of them. The reference to requests is what limits it;
-- there is no way to read a row for a request you are not part of.
create policy "request_photos_read" on public.request_photos
  for select using (exists (
    select 1 from public.requests r
     where r.id = request_id
       and (auth.uid() = r.requester_id or auth.uid() = r.assignee_id)));

create policy "request_photos_insert" on public.request_photos
  for insert with check (
    auth.uid() = user_id
    and exists (
      select 1 from public.requests r
       where r.id = request_id
         and (auth.uid() = r.requester_id or auth.uid() = r.assignee_id)));

-- you may take back your own picture; the other person's is theirs
create policy "request_photos_delete" on public.request_photos
  for delete using (auth.uid() = user_id);


-- ---------- PART 3 of 4 : reaching the other person's photos ----------
-- Files live at <uid>/request-<request>/<file>. The bucket's standing rule is
-- "your own folder only", which is right for shipment photos but would hide a
-- request photo from the very person it was taken for. This adds a narrower
-- second permission for request photos alone.
--
-- security definer because the check reads public.requests, which is private:
-- it answers one question about one path and nothing else.
create or replace function public.on_this_request(p text)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select exists (
    select 1 from public.requests r
     where 'request-' || r.id::text = (storage.foldername(p))[2]
       and (auth.uid() = r.requester_id or auth.uid() = r.assignee_id)
  );
$func$;

drop policy if exists "request_photos_team_select" on storage.objects;
drop policy if exists "request_photos_team_delete" on storage.objects;

create policy "request_photos_team_select" on storage.objects
  for select using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[2] like 'request-%'
    and public.on_this_request(name)
  );

-- Deleting is wider than the row rule on purpose: whoever is clearing up after
-- a finished request has to be able to remove both people's files, or the
-- pictures outlive the request that explained them.
create policy "request_photos_team_delete" on storage.objects
  for delete using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[2] like 'request-%'
    and public.on_this_request(name)
  );


-- ---------- PART 4 of 4 : clearing up after three days ----------
-- A completed request is worth keeping for a few days — long enough to check
-- what was asked and what came back — and worthless after that.
--
-- It hands back the storage paths it removed rather than just deleting rows,
-- because deleting a row in public.request_photos does not remove the file
-- itself. The app takes that list straight to the storage API. Reading the
-- paths before the delete is why this is plpgsql and not one statement.
--
-- security definer because the assignee may complete a request but may not
-- delete one, and either of them may be the person who opens the app first.
create or replace function public.purge_old_requests()
returns table(path text)
language plpgsql
security definer
set search_path = public
as $func$
declare
  doomed uuid[];
begin
  -- an anonymous caller cleans up nothing
  if not public.on_team(auth.uid()) then return; end if;

  select array_agg(r.id) into doomed
    from public.requests r
   where r.status = 'completed'
     and r.completed_at is not null
     and r.completed_at < now() - interval '3 days';

  if doomed is null then return; end if;

  return query
    select p.path from public.request_photos p where p.request_id = any(doomed);

  -- request_photos goes with it on the cascade
  delete from public.requests where id = any(doomed);
end $func$;

revoke all on function public.purge_old_requests() from public;
grant execute on function public.purge_old_requests() to authenticated;
