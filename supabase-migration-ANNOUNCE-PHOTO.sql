-- ============================================================
-- A PICTURE ON A POP-UP MESSAGE
-- Run in the Supabase SQL Editor AFTER supabase-migration-ANNOUNCE.sql.
-- Safe to run more than once, and safe to run as one batch.
--
-- Some things are quicker shown than described — where a pallet has been put,
-- which of two boxes is the right one, a shelf that needs clearing.
-- ============================================================

-- ---------- PART 1 of 2 : the column ----------
alter table public.announcements
  add column if not exists photo_path text;


-- ---------- PART 2 of 2 : letting the team see it ----------
-- The image sits at <uid>/announce-<id>/<file>, in the folder of whichever
-- admin sent it, and the bucket's standing rule is "your own folder only" —
-- so without this everybody else would get a broken square. Reading is opened
-- to the whole team, because a message goes to the whole team.
--
-- security definer because the check reads public.announcements, and it
-- answers one question about one path and nothing else.
create or replace function public.is_announce_photo(p text)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select exists (
    select 1 from public.announcements a
     where 'announce-' || a.id::text = (storage.foldername(p))[2]
  );
$func$;

drop policy if exists "announce_photos_team_select" on storage.objects;
drop policy if exists "announce_photos_team_delete" on storage.objects;

create policy "announce_photos_team_select" on storage.objects
  for select using (
    bucket_id = 'shipment-photos'
    and public.on_team(auth.uid())
    and public.is_announce_photo(name)
  );

-- an admin deleting a message should not leave its picture behind for good
create policy "announce_photos_team_delete" on storage.objects
  for delete using (
    bucket_id = 'shipment-photos'
    and public.is_admin(auth.uid())
    and public.is_announce_photo(name)
  );

-- Uploading needs no new rule: the image goes into the sender's own folder,
-- which the bucket already allows.


-- ---------- check ----------
--   select id, title, photo_path from public.announcements
--    order by created_at desc limit 5;
