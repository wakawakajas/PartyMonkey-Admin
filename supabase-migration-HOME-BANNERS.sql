-- ============================================================
-- HOME SCREEN: BANNERS AND EACH PERSON'S OWN ICON LAYOUT
-- Run in the Supabase SQL Editor AFTER supabase-migration-ADHOC.sql
-- (which made public.on_team). Safe to run more than once.
--
-- Banners are the team's: anyone signed in can put a photo up, swap it or
-- take it down, and there are never more than three.
-- SUPERSEDED: supabase-migration-HOME-BANNERS-ADMIN.sql makes changing them
-- admin-only. If this file is ever run again, run that one after it. A photo taken down is
-- deleted from storage by the app, not kept.
-- The icon layout is each person's own: the order they dragged their icons
-- into and the size they picked, so it follows them to any phone.
-- ============================================================

-- ---------- PART 1 of 3 : the banners ----------
create table if not exists public.home_banners (
  id          uuid primary key default gen_random_uuid(),
  photo_path  text not null,
  position    int  not null default 0,
  created_by  uuid default auth.uid(),
  created_at  timestamptz not null default now()
);
alter table public.home_banners enable row level security;

drop policy if exists "home_banners_team_select" on public.home_banners;
drop policy if exists "home_banners_team_insert" on public.home_banners;
drop policy if exists "home_banners_team_update" on public.home_banners;
drop policy if exists "home_banners_team_delete" on public.home_banners;
create policy "home_banners_team_select" on public.home_banners
  for select using (public.on_team(auth.uid()));
create policy "home_banners_team_insert" on public.home_banners
  for insert with check (public.on_team(auth.uid()));
create policy "home_banners_team_update" on public.home_banners
  for update using (public.on_team(auth.uid()));
create policy "home_banners_team_delete" on public.home_banners
  for delete using (public.on_team(auth.uid()));

grant select, insert, update, delete on public.home_banners to authenticated;
grant select, insert, update, delete on public.home_banners to service_role;

-- three at most, held here rather than trusted to every phone
create or replace function public.home_banners_cap()
returns trigger
language plpgsql
as $func$
begin
  if (select count(*) from public.home_banners) >= 3 then
    raise exception 'There can be at most 3 banners';
  end if;
  return new;
end;
$func$;
drop trigger if exists home_banners_cap on public.home_banners;
create trigger home_banners_cap before insert on public.home_banners
  for each row execute function public.home_banners_cap();


-- ---------- PART 2 of 3 : the banner photos ----------
-- They live in the photo bucket under home-banners/, a folder that belongs to
-- the team rather than to whoever uploaded, so anyone can see, replace or
-- remove any of them.
drop policy if exists "home_banner_photos_select" on storage.objects;
drop policy if exists "home_banner_photos_insert" on storage.objects;
drop policy if exists "home_banner_photos_delete" on storage.objects;
create policy "home_banner_photos_select" on storage.objects
  for select using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[1] = 'home-banners'
    and public.on_team(auth.uid())
  );
create policy "home_banner_photos_insert" on storage.objects
  for insert with check (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[1] = 'home-banners'
    and public.on_team(auth.uid())
  );
create policy "home_banner_photos_delete" on storage.objects
  for delete using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[1] = 'home-banners'
    and public.on_team(auth.uid())
  );


-- ---------- PART 3 of 3 : each person's layout ----------
create table if not exists public.home_layouts (
  user_id     uuid primary key default auth.uid(),
  tile_order  jsonb not null default '[]'::jsonb,
  density     text  not null default 'default',
  updated_at  timestamptz not null default now()
);
alter table public.home_layouts enable row level security;

drop policy if exists "home_layouts_own" on public.home_layouts;
create policy "home_layouts_own" on public.home_layouts
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

grant select, insert, update, delete on public.home_layouts to authenticated;
grant select, insert, update, delete on public.home_layouts to service_role;


-- ---------- check ----------
--   select * from public.home_banners order by position;
--   select * from public.home_layouts;
