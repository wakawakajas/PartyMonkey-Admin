-- ============================================================
-- GIFT TAG — ONE-OFF DESIGNS SAVED FOR LATER
-- Run in the Supabase SQL Editor. Safe to run more than once.
--
-- A one-off design on a gift tag page (Festive, Custom, Sticker, Die Cut)
-- can be kept with "Save for later": a row here, shown on the page it was
-- saved on for the whole team, until somebody deletes it with x.
--
-- `design` is what the one-off is (name, words, hole, back, how it sits, its
-- bleed and cut line). `path` is its picture in the shipment-photos bucket,
-- under <uid>/magic/ - where Magic Create's pictures are, so the team can
-- already read and delete it - and, being a column called path, the orphan
-- sweep sees it is in use. Deleting the row is followed by deleting the
-- picture, from the app.
-- ============================================================

create table if not exists public.gt_saved_oneoffs (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  page       text not null check (page in ('festive','custom','sticker','combo')),
  name       text not null default '',
  path       text not null,
  design     jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists gt_saved_oneoffs_page on public.gt_saved_oneoffs(page, created_at);

alter table public.gt_saved_oneoffs enable row level security;

drop policy if exists "gt_saved_oneoffs_select" on public.gt_saved_oneoffs;
drop policy if exists "gt_saved_oneoffs_insert" on public.gt_saved_oneoffs;
drop policy if exists "gt_saved_oneoffs_update" on public.gt_saved_oneoffs;
drop policy if exists "gt_saved_oneoffs_delete" on public.gt_saved_oneoffs;

create policy "gt_saved_oneoffs_select" on public.gt_saved_oneoffs
  for select using (public.on_team(auth.uid()));
create policy "gt_saved_oneoffs_insert" on public.gt_saved_oneoffs
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "gt_saved_oneoffs_update" on public.gt_saved_oneoffs
  for update using (public.on_team(auth.uid()));
create policy "gt_saved_oneoffs_delete" on public.gt_saved_oneoffs
  for delete using (public.on_team(auth.uid()));

-- signed-in team only: no anon grant
grant select, insert, update, delete on public.gt_saved_oneoffs to authenticated;
grant select, insert, update, delete on public.gt_saved_oneoffs to service_role;
