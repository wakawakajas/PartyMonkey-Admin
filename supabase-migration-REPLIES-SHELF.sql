-- ============================================================
-- A SHELF OF PHOTOS, AND THE CONVERSATION SO FAR
-- Run in the Supabase SQL Editor AFTER supabase-migration-REPLIES-PHOTO.sql.
-- Safe to run more than once.
--
-- Two things the screen was missing once replies started going out with
-- pictures attached:
--
--   reply_photos   photos that belong to no fact — a photo of the actual
--                  parcel, the batch code on this week's box, a picture
--                  somebody took on the floor this morning. Put on a shelf,
--                  picked by hand, sent with whichever reply needs it.
--
--   the history    what was said before the message being answered. A buyer
--                  asking "so tomorrow?" is answerable only if you can see
--                  the three lines above it, and a draft written without them
--                  answers the wrong question confidently.
--
-- WHY THE SHELF IS A TABLE AND NOT JUST A PATH ON THE MESSAGE: the orphan
-- sweep finds a file in use by looking for text columns named `path` or
-- ending in `_path`, which is how a fact's photo protects itself. A path that
-- lived only inside reply_messages.photos — a jsonb array — would be invisible
-- to that check and swept a day later, taking the picture out of a reply that
-- had not gone yet. On the shelf it is a photo_path column like every other,
-- so it is safe by the same rule as everything else.
-- ============================================================

-- ---------- PART 1 of 4 : the shelf ----------
create table if not exists public.reply_photos (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  photo_path text not null,
  -- what it is, for the person choosing it. Not read by the draft: a shelf
  -- photo is picked by hand, so nothing has to match on its words.
  label text not null default '',
  -- when it was last sent with something, so the shelf can put what is
  -- actually used at the front
  used_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists reply_photos_recent_idx
  on public.reply_photos (used_at desc nulls last, created_at desc);

alter table public.reply_photos enable row level security;

drop policy if exists "team_select" on public.reply_photos;
drop policy if exists "team_insert" on public.reply_photos;
drop policy if exists "team_update" on public.reply_photos;
drop policy if exists "team_delete" on public.reply_photos;

create policy "team_select" on public.reply_photos
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.reply_photos
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.reply_photos
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.reply_photos
  for delete using (public.on_team(auth.uid()));


-- ---------- PART 2 of 4 : the conversation so far ----------
-- history        the lines Macro Studio last read out of that thread, oldest
--                first: [{"inbound": true, "text": "..."}]. Inbound means the
--                buyer said it.
-- history_wanted set when somebody presses Pull chat history. The poller looks
--                for rows wanted since they were last read.
-- history_at     when the lines in `history` were read, so a second press
--                fetches again rather than being answered by what is already
--                there.
alter table public.reply_messages
  add column if not exists history jsonb not null default '[]'::jsonb;
alter table public.reply_messages
  add column if not exists history_wanted_at timestamptz;
alter table public.reply_messages
  add column if not exists history_at timestamptz;

create index if not exists reply_messages_history_idx
  on public.reply_messages (history_wanted_at)
  where history_wanted_at is not null;


-- ---------- PART 3 of 4 : the sweep leaves the shelf alone ----------
-- Nothing to do: reply_photos.photo_path is a text column ending in _path, so
-- public.photo_columns() finds it on its own — the whole point of the sweep
-- asking the catalogue rather than being told. This part exists to say so, and
-- to give you the query that proves it:
--
--   select tbl, col from public.photo_columns() where tbl like 'reply%';
--
-- It should list reply_facts.photo_path and reply_photos.photo_path.


-- ---------- PART 4 of 4 : check ----------
--   select label, photo_path from public.reply_photos order by created_at desc;
--   select left(message, 30), jsonb_array_length(history) as lines
--     from public.reply_messages order by received_at desc limit 5;
