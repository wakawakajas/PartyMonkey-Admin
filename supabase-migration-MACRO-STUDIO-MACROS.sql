-- ============================================================
-- MACRO STUDIO — the macros themselves
-- Run in the Supabase SQL Editor AFTER supabase-migration-MACRO-STUDIO-STORAGE.sql.
-- Safe to run more than once.
--
-- The build download puts the program on a colleague's PC. This puts the
-- macros in it: one .json file per macro, published here once and picked
-- up by whoever needs it, instead of the file going round by chat and
-- somebody running last week's copy.
--
-- Kept in its own bucket rather than a folder beside the build, so that
-- "who may publish a macro" can differ from "who may publish the program"
-- later without untangling one bucket's policies from the other's.
-- ============================================================

insert into storage.buckets (id, name, public)
values ('macro-studio-macros', 'macro-studio-macros', false)
on conflict (id) do nothing;

drop policy if exists "macro_studio_macros_select" on storage.objects;
drop policy if exists "macro_studio_macros_insert" on storage.objects;
drop policy if exists "macro_studio_macros_update" on storage.objects;
drop policy if exists "macro_studio_macros_delete" on storage.objects;

-- anyone granted Macro Studio access can take a copy
create policy "macro_studio_macros_select" on storage.objects
  for select using (
    bucket_id = 'macro-studio-macros'
    and (public.on_macro_studio_team(auth.uid()) or public.is_admin(auth.uid()))
  );

-- only an admin publishes them: everyone downloading the same shared copy
-- is the point, and a macro replaced by mistake is a macro everybody runs
create policy "macro_studio_macros_insert" on storage.objects
  for insert with check (bucket_id = 'macro-studio-macros' and public.is_admin(auth.uid()));
create policy "macro_studio_macros_update" on storage.objects
  for update using (bucket_id = 'macro-studio-macros' and public.is_admin(auth.uid()));
create policy "macro_studio_macros_delete" on storage.objects
  for delete using (bucket_id = 'macro-studio-macros' and public.is_admin(auth.uid()));

-- ---------- check ----------
-- What's published right now:
--   select name, updated_at from storage.objects
--    where bucket_id = 'macro-studio-macros' order by name;
