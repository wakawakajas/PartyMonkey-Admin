-- One-time migration for an existing project that already ran
-- supabase-schema.sql before sort_order existed. Run this once in the
-- Supabase SQL Editor (Project → SQL Editor → New query). Safe to run even
-- if the column already exists.

alter table public.templates add column if not exists sort_order integer not null default 0;

-- backfill: give existing templates a stable order based on creation time,
-- per user, so nothing looks shuffled the first time you open the app
update public.templates t
set sort_order = sub.rn
from (
  select id, row_number() over (partition by user_id order by created_at) - 1 as rn
  from public.templates
) sub
where t.id = sub.id;
