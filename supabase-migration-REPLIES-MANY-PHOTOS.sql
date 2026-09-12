-- ============================================================
-- A FACT CAN CARRY SEVERAL PHOTOS
-- Run in the Supabase SQL Editor AFTER supabase-migration-REPLIES-SHELF.sql.
-- Safe to run more than once.
--
-- One photo per fact was one photo too few. "What sizes do you have" is
-- answered by the size chart AND the measuring guide; "how do I care for it"
-- by the care label and the washing symbols. Splitting those into two facts
-- to carry two pictures would be inventing facts to hold files.
--
-- photo_path stays and keeps working: it is the first photo, and every fact
-- that already had one is copied into the new list below. Nothing has to be
-- re-uploaded.
-- ============================================================

-- ---------- PART 1 of 3 : the column ----------
alter table public.reply_facts
  add column if not exists photos jsonb not null default '[]'::jsonb;


-- ---------- PART 2 of 3 : what is already there ----------
-- Every fact with a photo gets that same photo as the first entry of its list,
-- so nothing looks lost the moment the app starts reading the new column.
-- Written to skip rows that already have a list, which is what makes this safe
-- to run twice.
update public.reply_facts
   set photos = jsonb_build_array(photo_path)
 where coalesce(photo_path, '') <> ''
   and jsonb_array_length(photos) = 0;


-- ---------- PART 3 of 3 : check ----------
--   select left(fact, 40), jsonb_array_length(photos) as pics from public.reply_facts;
--
-- The orphan sweep still protects these: photo_path holds the first one, and
-- the sweep finds any text column named path or ending in _path. A second or
-- third photo lives only inside the jsonb, which the sweep cannot see -- so
-- the app writes every one of them to the shelf as well (reply_photos, whose
-- photo_path column the sweep does see). That is why a fact's extra photos
-- show up on the shelf too, and why deleting them from the shelf is what
-- actually deletes the file.
