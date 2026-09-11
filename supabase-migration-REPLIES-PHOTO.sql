-- ============================================================
-- A FACT CAN CARRY A PHOTO
-- Run in the Supabase SQL Editor AFTER supabase-migration-REPLIES.sql.
-- Safe to run more than once.
--
-- Half the questions a shop gets are answered by a picture rather than a
-- sentence: the size chart, the care label, what the four colours actually
-- look like, where the batch code is printed. Typed out they take three
-- messages and still get asked again.
--
-- So a fact may have a photo attached to it. The fact is still what the draft
-- is allowed to say; the photo is what goes with it. When the buyer's question
-- matches a fact that has one, the screen offers the picture beside the draft,
-- and on the shop PC Macro Studio pastes it into the chat after the words.
--
-- WHY ON THE FACT AND NOT ON THE MESSAGE: a size chart is true of the shop,
-- not of one conversation. Attached to the fact it is uploaded once and offered
-- every time the question comes round again, which is the same reason the facts
-- exist at all.
-- ============================================================

-- ---------- PART 1 of 3 : the columns ----------
-- The path in the shipment-photos bucket, or '' for a fact that is only words.
alter table public.reply_facts
  add column if not exists photo_path text not null default '';

-- Which photos this reply is sending with it: an array of bucket paths, chosen
-- on the screen before Enter. Written down rather than worked out again later,
-- because what was actually sent is a fact about the past and the matching
-- would not necessarily pick the same photo tomorrow.
alter table public.reply_messages
  add column if not exists photos jsonb not null default '[]'::jsonb;


-- ---------- PART 2 of 3 : who may see the photos ----------
-- Reply photos live at <uid>/replies/<file>. The bucket's standing rule is
-- "your own folder only", which would hide a shop's size chart from everybody
-- except whoever happened to upload it — so, exactly like the pick up photos,
-- this adds a second narrower permission for that one folder name. Shipment
-- and box photos stay private to whoever uploaded them.
drop policy if exists "reply_photos_team_select" on storage.objects;
drop policy if exists "reply_photos_team_delete" on storage.objects;

create policy "reply_photos_team_select" on storage.objects
  for select using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[2] = 'replies'
    and public.on_team(auth.uid())
  );

create policy "reply_photos_team_delete" on storage.objects
  for delete using (
    bucket_id = 'shipment-photos'
    and (storage.foldername(name))[2] = 'replies'
    and public.on_team(auth.uid())
  );


-- ---------- PART 3 of 3 : check ----------
--   select left(fact, 40), photo_path <> '' as has_photo from public.reply_facts;
--   select policyname from pg_policies
--    where schemaname = 'storage' and policyname like 'reply_photos%';
