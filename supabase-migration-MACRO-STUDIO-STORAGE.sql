-- ============================================================
-- MACRO STUDIO — build download/upload
-- Run in the Supabase SQL Editor AFTER supabase-migration-MACRO-STUDIO.sql.
-- Safe to run more than once.
--
-- Lets an admin upload the Macro Studio .zip once, and anyone granted
-- can_macro_studio download it. The file lives in Pigu (this app), not
-- inside Macro Studio's own local UI, because a colleague who doesn't
-- have Macro Studio installed yet has no way to reach that UI -- this
-- is what gets it onto their PC in the first place.
-- ============================================================

insert into storage.buckets (id, name, public)
values ('macro-studio-builds', 'macro-studio-builds', false)
on conflict (id) do nothing;

create or replace function public.on_macro_studio_team(uid uuid)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select coalesce((select can_macro_studio from public.profiles where user_id = uid), false);
$func$;

drop policy if exists "macro_studio_builds_select" on storage.objects;
drop policy if exists "macro_studio_builds_insert" on storage.objects;
drop policy if exists "macro_studio_builds_update" on storage.objects;
drop policy if exists "macro_studio_builds_delete" on storage.objects;

-- anyone granted Macro Studio access can download the current build
create policy "macro_studio_builds_select" on storage.objects
  for select using (
    bucket_id = 'macro-studio-builds'
    and (public.on_macro_studio_team(auth.uid()) or public.is_admin(auth.uid()))
  );

-- only an admin publishes/replaces it -- one shared file, not per-user
create policy "macro_studio_builds_insert" on storage.objects
  for insert with check (bucket_id = 'macro-studio-builds' and public.is_admin(auth.uid()));
create policy "macro_studio_builds_update" on storage.objects
  for update using (bucket_id = 'macro-studio-builds' and public.is_admin(auth.uid()));
create policy "macro_studio_builds_delete" on storage.objects
  for delete using (bucket_id = 'macro-studio-builds' and public.is_admin(auth.uid()));

-- ---------- check ----------
-- What's currently uploaded:
--   select * from storage.objects where bucket_id = 'macro-studio-builds';
