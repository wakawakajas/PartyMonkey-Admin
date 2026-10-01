-- ============================================================
-- FINANCE — PAPER & LAMINATE COSTS
-- Run in the Supabase SQL Editor AFTER supabase-migration-FINANCE.sql.
-- Safe to run more than once.
--
-- The cost of a sheet: the paper, the click to print it, and the laminate
-- by the cm. Shop knowledge rather than one PC's, so it lives here and not in
-- localStorage. One row, like calc_settings; the two tables are lists so a
-- paper can be added or dropped without another migration.
-- ============================================================

create or replace function public.on_finance_team(uid uuid)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select coalesce((select can_finance from public.profiles where user_id = uid), false);
$func$;

create table if not exists public.finance_settings (
  id boolean primary key default true check (id),
  -- the header of each paper column, editable because the sheet they came
  -- from left two of them unnamed
  paper_columns jsonb not null default
    '["Paper","GSM","Size","Paper Cost","Print Cost","Notes",""]'::jsonb,
  -- [{"cells": ["SR A3 - Japan White A Art card","260","320mm x 450mm","0.11","0.10","","0.28"]}, ...]
  papers jsonb not null default '[]'::jsonb,
  -- [{"name": "Matte Laminate (HOT)", "roll_cm": 50000, "roll_cost": 86.11}, ...]
  laminates jsonb not null default '[]'::jsonb,
  updated_at timestamptz not null default now(),
  updated_by uuid references auth.users(id)
);

insert into public.finance_settings(id, papers, laminates) values (true,
'[
 {"cells":["SR A3 - Japan White A Art card","260","320mm x 450mm","0.11","0.10","","0.28"]},
 {"cells":["SR A3 - Japan Matte Postcard","256","317.5mm x 450mm","0.12","0.10","","0.25"]},
 {"cells":["SRA3 - Japan TK Matte Art Card","300","317.5mm x 450mm","0.14","0.10","","0.31"]},
 {"cells":["SRA3 - Japan White A Art Card","310","320mm x 450mm","0.13","0.10","","0.33"]},
 {"cells":["SRA3 MirrorKote Sticker Paper (White Back)","87","317.5mm x 450mm","0.19","0.10","","0.18"]},
 {"cells":["Brochure","128","317.5mm x 450mm","0.04","0.10","",""]},
 {"cells":["Brochure","157","317.5mm x 450mm","0.05","0.10","",""]},
 {"cells":["Cavallo Plus (Gloss)","250","317mm x 450mm","0.10","0.10","","0.27"]},
 {"cells":["Cavallo Plus (Gloss)","300","317mm x 450mm","0.11","0.10","","0.34"]},
 {"cells":["Green Forest Digital","250","317mm x 450mm","0.14","0.10","","0.27"]},
 {"cells":["Green Forest Digital","300","317mm x 450mm","0.17","0.10","","0.33"]},
 {"cells":["DigiPrint Kraft Sticker","80","325mm x 485mm","0.20","0.10","","0.18"]},
 {"cells":["DigiPrint CastCoated Sticker","190","325mm x 485mm","0.16","0.10","","0.18"]},
 {"cells":["DigiPrint Woodfree Sticker","80","325mm x 485mm","0.15","0.10","","0.18"]},
 {"cells":["PSP Digi-Matt","250","317mm x 450mm","0.11","0.10","",""]},
 {"cells":["PSP Digi-Matt","300","317mm x 450mm","0.12","0.10","0",""]},
 {"cells":["Green Buddy Digi-Gloss","157","317mm x 450mm","0.06","0.10","",""]},
 {"cells":["GOLDEAST GLOSS (PEFC)","157","317.5mm x 900mm","0.12","0.20","",""]}
]'::jsonb,
'[
 {"name":"Matte Laminate (COLD)","roll_cm":20000,"roll_cost":163.5},
 {"name":"Matte Laminate (HOT)","roll_cm":50000,"roll_cost":86.11},
 {"name":"Gloss Laminate (HOT)","roll_cm":50000,"roll_cost":69.76},
 {"name":"Gloss Laminate (COLD)","roll_cm":20000,"roll_cost":103.55}
]'::jsonb)
on conflict (id) do nothing;

-- width in px of each paper column, dragged in the app; [] = the defaults
alter table public.finance_settings add column if not exists paper_col_widths jsonb not null default '[]'::jsonb;

alter table public.finance_settings enable row level security;

drop policy if exists "finance_settings_read"  on public.finance_settings;
drop policy if exists "finance_settings_write" on public.finance_settings;

create policy "finance_settings_read" on public.finance_settings
  for select using (public.on_finance_team(auth.uid()) or public.is_admin(auth.uid()));
create policy "finance_settings_write" on public.finance_settings
  for update using (public.on_finance_team(auth.uid()) or public.is_admin(auth.uid()));

-- check:
--   select paper_columns, jsonb_array_length(papers), laminates from public.finance_settings;
