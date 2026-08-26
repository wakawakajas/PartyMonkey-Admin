-- ============================================================
-- DAILY PICK LIST: ONE ACCENT, AND IT IS BRITISH
-- Run in the Supabase SQL Editor AFTER supabase-migration-PICK-LIST-TOPUP.sql
-- and supabase-migration-PICK-LIST-USER-VOICE.sql.
-- Safe to run more than once.
--
-- The voice list used to offer Singapore, America and a spread of accents
-- underneath them. It offers British and nothing else now: a sheet read in
-- one accent all morning is a sheet a picker stops having to listen to, and
-- two floors reading the same SKU name two different ways is the opposite of
-- that.
--
-- So anything still holding one of the accents that were taken away is put
-- back to "whatever British voice this phone has". The voice's name goes with
-- it — a name and an accent are one choice, and half of a choice nobody can
-- now make is nobody's voice.
-- ============================================================

-- ---------- the shops ----------
alter table public.pick_list_voice
  alter column voice_lang set default 'en-GB';

update public.pick_list_voice
   set voice_lang = 'en-GB',
       voice_name = null,
       updated_at = now()
 where voice_lang is null
    or lower(voice_lang) not like 'en-gb%';

-- ---------- and the people given a voice of their own ----------
-- One column holding accent|name|pitch|speed. The accent is the first field,
-- so a row whose first field is not British is a choice off the old list:
-- cleared back to "the shop's own setting", which an admin can change again
-- on Users & access from the British list.
update public.profiles
   set pl_voice = null
 where pl_voice is not null
   and lower(split_part(pl_voice, '|', 1)) not like 'en-gb%';
