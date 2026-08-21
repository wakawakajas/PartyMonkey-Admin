-- ============================================================
-- DAILY PICK LIST: TOP UP, THE WHITELIST, AND A VOICE OF ITS OWN
-- Run in the Supabase SQL Editor AFTER supabase-migration-DAILY-PICK-LIST.sql,
-- supabase-migration-ADHOC-FROM-PICK-LIST.sql, supabase-migration-PICK-LIST-HIDDEN2.sql
-- and supabase-migration-PICK-LIST-VOICE.sql.
-- Safe to run more than once, and safe to run as one batch.
--
-- Three things the floor asked for:
--   1. Top Up. A line can be picked and still be short on the shelf behind
--      it, so "somebody refill this" is a second thing said about a line
--      rather than a fourth answer instead of the first three.
--   2. A whitelist. Hiding "Gold-2" also hides "Gold-2cm Ribbon", which is a
--      real SKU somebody has to pick. The whitelist names the exceptions.
--   3. Pitch and a named voice, so the phone reading a hundred lines a
--      morning can be set to something the floor can actually hear.
-- ============================================================

-- ---------- PART 1 of 6 : Top Up on a pick list line ----------
-- A column of its own rather than a fourth value in status, because it is
-- allowed alongside any of the three: a line can be OK and still need topping
-- up, and folding the two together would lose whichever was tapped second.
alter table public.pick_list_items
  add column if not exists top_up boolean not null default false;

-- And whether the refill has actually been done, which is a different fact
-- from having asked for it. Set when the Urgent Top Up job is ticked off on
-- the Ad-Hoc Picking page; that is what "Topped Up" on the sheet means.
alter table public.pick_list_items
  add column if not exists top_up_done boolean not null default false;

-- what the bubble and the Urgent Top Up section ask for
create index if not exists pick_list_items_top_up_idx
  on public.pick_list_items(list_id)
  where top_up;


-- ---------- PART 2 of 6 : Urgent Top Up on the sheet's own list ----------
-- A sheet raises one list on the Ad-Hoc Picking page, whatever is put on it.
-- A refill and an ad-hoc job are two kinds of work off the same morning, and
-- two lists for one sheet would mean checking two places for it — so they
-- share the list and this flag tells them apart inside it, under their own
-- headings.
--
-- It is also what lets Top Up be taken back off a pick list line without
-- disturbing the ad-hoc job that line may also have raised: both point at the
-- same pick_item_id, and only one of them carries the flag.
alter table public.adhoc_items
  add column if not exists top_up boolean not null default false;

-- An earlier version of this file added the same column to adhoc_orders, for
-- a separate refill list that no longer exists. Nothing reads it now. It is
-- left where it is rather than dropped: a column nobody asks about costs
-- nothing, and dropping one is the sort of thing that cannot be undone.


-- ---------- PART 3 of 6 : the whitelist ----------
-- Hidden words match anywhere in a SKU name, which is what makes one word
-- enough to hide a whole class of printed-to-order lines — and also what
-- makes "Gold-2" swallow "Gold-2cm Ribbon". A whitelist entry is a whole SKU
-- name, matched exactly rather than as a substring: it says "this one line,
-- whatever it contains, is picked off a shelf like any other".
--
-- One list for both shops, unlike the hidden words. A hidden word is about
-- what a shop stocks; an exact SKU name belongs to whichever shop sells it
-- and cannot collide with the other's.
create table if not exists public.pick_list_whitelist_skus (
  id uuid primary key default gen_random_uuid(),
  -- nullable, the same as the other two lists: an entry should not go because
  -- the person who typed it has left
  user_id uuid references auth.users(id) on delete set null,
  term text not null check (length(trim(term)) > 0),
  created_at timestamptz not null default now()
);

create unique index if not exists pick_list_whitelist_skus_term_key
  on public.pick_list_whitelist_skus(lower(trim(term)));

alter table public.pick_list_whitelist_skus enable row level security;

drop policy if exists "team_select" on public.pick_list_whitelist_skus;
drop policy if exists "team_insert" on public.pick_list_whitelist_skus;
drop policy if exists "team_update" on public.pick_list_whitelist_skus;
drop policy if exists "team_delete" on public.pick_list_whitelist_skus;

create policy "team_select" on public.pick_list_whitelist_skus
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.pick_list_whitelist_skus
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.pick_list_whitelist_skus
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.pick_list_whitelist_skus
  for delete using (public.on_team(auth.uid()));

alter table public.pick_list_whitelist_skus replica identity full;


-- ---------- PART 4 of 6 : how the voice sounds ----------
-- Speed was already here. Pitch and the voice itself were not, and between
-- them they are the difference between a voice a warehouse can pick to all
-- morning and one nobody can make out over the machinery.
alter table public.pick_list_voice
  add column if not exists pitch numeric not null default 1.15;

alter table public.pick_list_voice
  drop constraint if exists pick_list_voice_pitch_ck;
alter table public.pick_list_voice
  add constraint pick_list_voice_pitch_ck check (pitch >= 0.6 and pitch <= 1.8);

-- Which installed voice to ask for, by the name the phone knows it as. Null
-- means "whatever this phone has that sounds closest", which is what every
-- phone did before this column existed. The accent is asked for separately
-- because a phone that has no voice of that name may still have one of that
-- accent, and an accent is the half that matters when it does.
alter table public.pick_list_voice
  add column if not exists voice_name text;
alter table public.pick_list_voice
  add column if not exists voice_lang text not null default 'en-SG';


-- ---------- PART 5 of 6 : tidying up the separate refill lists ----------
-- An earlier version of this file put refills on a list of their own, named
-- "Urgent Top Up - <sheet>". They belong on the sheet's own list now, under
-- their own heading inside it, so anything still sitting on one of those is
-- moved across and the empty list taken away.
--
-- Safe to run more than once: after the first run there is nothing left to
-- find, and every statement here is written to do nothing in that case.
update public.adhoc_items i
   set order_ref = keep.id
  from public.adhoc_orders old
  join public.adhoc_orders keep
    on keep.pick_list_id = old.pick_list_id
   and coalesce(keep.top_up, false) = false
   and keep.deleted_at is null
 where i.order_ref = old.id
   and coalesce(old.top_up, false) = true;

delete from public.adhoc_orders o
 where coalesce(o.top_up, false) = true
   and not exists (select 1 from public.adhoc_items where order_ref = o.id);

-- And a refill list whose sheet never raised an ordinary one has nowhere to
-- be moved to, so it becomes the sheet's own list instead of being thrown
-- away with the work on it.
update public.adhoc_orders o
   set top_up = false,
       order_id = coalesce(
         (select l.title from public.pick_lists l where l.id = o.pick_list_id),
         o.order_id)
 where coalesce(o.top_up, false) = true;


-- ---------- PART 6 of 6 : live updates ----------
-- Top Up is tapped on one phone and has to show on the other three, the same
-- as every other status on a sheet being picked by four people at once.
alter table public.pick_list_items replica identity full;
alter table public.adhoc_items     replica identity full;
alter table public.adhoc_orders    replica identity full;
