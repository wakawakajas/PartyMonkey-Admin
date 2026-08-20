-- ============================================================
-- DAILY PICK LIST: A HIDDEN LIST PER SHOP, AND WORDS THE VOICE SKIPS
-- Run in the Supabase SQL Editor AFTER supabase-migration-PICK-LIST-HIDDEN.sql.
-- Safe to run more than once.
--
-- Two things:
--   1. The hidden SKUs are per shop. PartyMonkey and Plant Talks stock
--      different things and print different things to order, so one list
--      between them was always going to hide the wrong lines on one of the
--      two sheets. Which shop a sheet belongs to is read off its file name,
--      the same PT rule that decides where its ad-hoc work is filed.
--   2. A list of words the voice leaves out when it reads a line aloud.
--      Packaging noise — "1pc", "12Inch" — is worth seeing on the screen and
--      not worth hearing on every line of a hundred-line sheet.
-- ============================================================

-- ---------- PART 1 of 3 : whose list is whose ----------
-- Everything already on the list was entered before there were two shops, and
-- PartyMonkey is where it was entered — the same call supabase-migration-STORES
-- made for the pick lists themselves.
alter table public.pick_list_hidden_skus
  add column if not exists store text not null default 'partymonkey';

create index if not exists pick_list_hidden_skus_store_idx
  on public.pick_list_hidden_skus(store);

-- A word only has to be unique within its own shop now: both of them may well
-- want to hide "Custom", and neither should block the other.
drop index if exists pick_list_hidden_skus_term_key;
create unique index if not exists pick_list_hidden_skus_store_term_key
  on public.pick_list_hidden_skus(store, lower(trim(term)));

-- Plant Talks starts where PartyMonkey started, so the day this runs changes
-- nothing for either of them. Only ever on a shop with an empty list:
-- somebody who has cleared Plant Talks' list wants it cleared, and running
-- the file again must not put "Custom" back under them.
insert into public.pick_list_hidden_skus (term, store)
select 'Custom','planttalks'
 where not exists (
   select 1 from public.pick_list_hidden_skus where store = 'planttalks');


-- ---------- PART 2 of 3 : what the voice leaves out ----------
-- Not per shop, unlike the list above: this is about how a name sounds read
-- aloud rather than about what either shop stocks, and "1pc" is noise on
-- anybody's sheet.
create table if not exists public.pick_list_unspoken (
  id uuid primary key default gen_random_uuid(),
  -- nullable for the same reason as the hidden list: a word should not go
  -- because the person who typed it has left
  user_id uuid references auth.users(id) on delete set null,
  term text not null check (length(trim(term)) > 0),
  created_at timestamptz not null default now()
);

create unique index if not exists pick_list_unspoken_term_key
  on public.pick_list_unspoken(lower(trim(term)));

alter table public.pick_list_unspoken enable row level security;

drop policy if exists "team_select" on public.pick_list_unspoken;
drop policy if exists "team_insert" on public.pick_list_unspoken;
drop policy if exists "team_update" on public.pick_list_unspoken;
drop policy if exists "team_delete" on public.pick_list_unspoken;

create policy "team_select" on public.pick_list_unspoken
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.pick_list_unspoken
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.pick_list_unspoken
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.pick_list_unspoken
  for delete using (public.on_team(auth.uid()));


-- ---------- PART 3 of 3 : live updates ----------
alter table public.pick_list_unspoken replica identity full;
