-- ============================================================
-- REPLIES — the drafted answer to a buyer's chat message
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql
-- (which is where public.on_team comes from) and AFTER
-- supabase-migration-ADHOC.sql if that has not been run either.
-- Safe to run more than once, and safe to run as one batch.
--
-- Answering Shopee chat is the same twenty questions every day — where is my
-- parcel, is this in stock, can it be cheaper — typed out again each time in
-- whatever language the buyer used. The Replies screen drafts the answer and
-- waits: you read it, press Enter, and it goes. Nothing is ever sent without
-- that press.
--
-- Three tables, and they are three different kinds of thing:
--
--   reply_facts     what is true about the shop. Typed once, or distilled out
--                   of replies already sent. This is what keeps a draft from
--                   inventing a shipping date.
--   reply_examples  replies that were actually approved, buyer's words and
--                   yours. The draft is written in the voice of these, so the
--                   longer it runs the more it sounds like you and the less it
--                   sounds like a model.
--   reply_messages  the chat messages themselves, as Macro Studio reads them
--                   out of DuoKe on the shop PC — and the approved reply going
--                   back the other way, for Macro Studio to type in.
--
-- All three are shared with the team, like the shop floor lists and unlike
-- orders or print templates: the person at the PC in the morning is not
-- necessarily the person who answered yesterday, and a shop has one voice.
-- ============================================================

-- ---------- PART 1 of 6 : the door ----------
-- defaults to false, so nobody gains it without being given it
alter table public.profiles
  add column if not exists can_replies boolean not null default false;

-- Who can open it — grant it from Users & access, or here:
--   update public.profiles set can_replies = true where email = 'someone@example.com';


-- ---------- PART 2 of 6 : what is true about the shop ----------
create table if not exists public.reply_facts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  fact text not null,
  -- 'typed'     somebody wrote it on the Replies screen
  -- 'distilled' it was read back out of replies already approved, and is
  --             worth a second look before it is trusted
  source text not null default 'typed' check (source in ('typed','distilled')),
  created_at timestamptz not null default now()
);

-- The same fact typed twice is one fact. Case and surrounding spaces are not
-- what makes it a different fact, so the key is on the folded form.
create unique index if not exists reply_facts_fact_key
  on public.reply_facts (lower(btrim(fact)));

alter table public.reply_facts enable row level security;


-- ---------- PART 3 of 6 : replies that were approved ----------
-- hits is how often this example has been handed to a draft. It is the only
-- ranking there is: the reply that keeps being useful rises, and the one
-- nobody's question ever looks like sinks and is eventually pruned.
--
-- edited says the draft was changed before it was sent, which makes this row
-- the more valuable kind of example — it is a correction, not a confirmation.
create table if not exists public.reply_examples (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  buyer_text text not null,
  reply_text text not null,
  lang text not null default '',
  hits integer not null default 0,
  edited boolean not null default false,
  created_at timestamptz not null default now(),
  used_at timestamptz
);

create index if not exists reply_examples_rank_idx
  on public.reply_examples (hits desc, created_at desc);

alter table public.reply_examples enable row level security;


-- ---------- PART 4 of 6 : the messages, both directions ----------
-- One row per buyer message. It arrives 'new', gets a draft, and ends either
-- 'answered' (the reply is written down, and on the shop PC it has been typed
-- back into DuoKe) or 'skipped' (dealt with by hand, or not worth answering).
--
-- chat_key is how Macro Studio finds the same conversation again — whatever
-- DuoKe shows as the thread's own name. buyer is what to call them on screen.
--
-- fingerprint is the same message read twice. The poller keeps its own note of
-- what it has already sent, but a reinstall, a cleared file or two agents
-- running at once would otherwise fill the list with duplicates, so the
-- database refuses them outright. It is the agent's hash of the thread and the
-- text, not something anybody reads.
--
-- sent_at is DuoKe's own timestamp where there is one to read; received_at is
-- when this row was written. They are different questions and the list is
-- ordered by the first, falling back to the second.
create table if not exists public.reply_messages (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  store text not null default '',
  chat_key text not null default '',
  buyer text not null default '',
  message text not null,
  fingerprint text not null,
  -- 'duoke' read off the shop PC, 'paste' typed into the screen by hand
  source text not null default 'duoke' check (source in ('duoke','paste')),
  status text not null default 'new' check (status in ('new','answered','skipped')),
  draft text not null default '',
  reply text not null default '',
  -- set when a reply is approved; the poller looks for answered rows whose
  -- reply it has not typed yet, and stamps typed_at when it has
  answered_at timestamptz,
  typed_at timestamptz,
  sent_at timestamptz,
  received_at timestamptz not null default now()
);

create unique index if not exists reply_messages_fingerprint_key
  on public.reply_messages (fingerprint);

-- the list's own order, and the poller's two questions
create index if not exists reply_messages_open_idx
  on public.reply_messages (status, received_at desc);
create index if not exists reply_messages_totype_idx
  on public.reply_messages (status, typed_at);

alter table public.reply_messages enable row level security;


-- ---------- PART 5 of 6 : shared with the team ----------
drop policy if exists "team_select" on public.reply_facts;
drop policy if exists "team_insert" on public.reply_facts;
drop policy if exists "team_update" on public.reply_facts;
drop policy if exists "team_delete" on public.reply_facts;

create policy "team_select" on public.reply_facts
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.reply_facts
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.reply_facts
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.reply_facts
  for delete using (public.on_team(auth.uid()));

drop policy if exists "team_select" on public.reply_examples;
drop policy if exists "team_insert" on public.reply_examples;
drop policy if exists "team_update" on public.reply_examples;
drop policy if exists "team_delete" on public.reply_examples;

create policy "team_select" on public.reply_examples
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.reply_examples
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.reply_examples
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.reply_examples
  for delete using (public.on_team(auth.uid()));

drop policy if exists "team_select" on public.reply_messages;
drop policy if exists "team_insert" on public.reply_messages;
drop policy if exists "team_update" on public.reply_messages;
drop policy if exists "team_delete" on public.reply_messages;

create policy "team_select" on public.reply_messages
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.reply_messages
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.reply_messages
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.reply_messages
  for delete using (public.on_team(auth.uid()));


-- ---------- PART 6 of 7 : is the shop PC still reading ----------
-- One row, forever. Macro Studio stamps it every time round its loop, and the
-- Replies screen reads it to answer the only question worth asking about a
-- sync: is it running right now, and did it find DuoKe when it last looked.
--
-- Without this the screen cannot tell "no new messages this morning" from
-- "that PC has been asleep since Friday", and those need opposite reactions.
create table if not exists public.reply_sync (
  id boolean primary key default true check (id),
  device text not null default '',
  -- what the last pass actually managed: found the window, read N threads
  window_found boolean not null default false,
  threads integer not null default 0,
  note text not null default '',
  beat_at timestamptz not null default now()
);

insert into public.reply_sync (id) values (true) on conflict (id) do nothing;

alter table public.reply_sync enable row level security;

drop policy if exists "team_select" on public.reply_sync;
drop policy if exists "team_update" on public.reply_sync;
drop policy if exists "team_insert" on public.reply_sync;

create policy "team_select" on public.reply_sync
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.reply_sync
  for insert with check (public.on_team(auth.uid()));
create policy "team_update" on public.reply_sync
  for update using (public.on_team(auth.uid()));


-- ---------- PART 7 of 7 : live updates between devices ----------
-- A message read off the shop PC has to appear on the phone in the kitchen
-- without anybody pulling to refresh, and a reply approved on the phone has
-- to leave the list on the PC. Both are the same subscription.
alter table public.reply_messages replica identity full;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'reply_messages')
  then
    alter publication supabase_realtime add table public.reply_messages;
  end if;
end $$;


-- ---------- check ----------
--   select count(*) from public.reply_messages where status = 'new';
--   select fact from public.reply_facts order by created_at;
--   select hits, left(buyer_text, 40) from public.reply_examples
--    order by hits desc limit 10;
