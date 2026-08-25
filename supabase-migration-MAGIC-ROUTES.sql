-- ============================================================
-- MAGIC CREATE — ROUTING
-- Run in the Supabase SQL Editor AFTER supabase-migration-MAGIC-CREATE.sql
-- (and the ACCESS/ADHOC files it names, which is where public.on_team comes
-- from). Safe to run more than once, and safe to run as one batch.
--
-- Which tool a SKU on the summary is added with used to be worked out from
-- where its artwork sat: <SKU>.png in the designs folder was a print job, a
-- design of that name on a gift tag page was a tag. A shop whose gift tag
-- SKUs also have a PNG of the same name has no way to say so, and the answer
-- was wrong a hundred cards at a time.
--
-- So the shop says it here. A rule is a piece of text and where anything
-- carrying it goes: "Gift Tag" -> the Custom page. An override is one SKU
-- named outright, and beats every rule. Both are shared, because two people
-- working the same morning's batch must add the same SKU to the same sheet.
-- ============================================================

-- ---------- PART 1 of 4 : the table ----------
-- kind   'rule'  match is looked for inside the SKU (or the variation)
--        'sku'   match is one SKU exactly, and is the last word on it
-- how    how a rule's text is compared. Ignored for an override.
-- dest   'print'    the print sheet, on the template its folder belongs to
--        'festive'  the Festive gift tag page's sheet
--        'custom'   the Custom gift tag page's sheet
--        'skip'     never added by Add all — handled off the sheet
create table if not exists public.magic_routes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  kind text not null default 'rule' check (kind in ('rule','sku')),
  match text not null default '',
  how text not null default 'contains' check (how in ('contains','starts','exact')),
  dest text not null check (dest in ('print','festive','custom','skip')),
  -- rules are read in this order and the first one that fits wins, so the
  -- order is the rule as much as the text is
  sort_order integer not null default 0,
  created_at timestamptz not null default now()
);

-- one override to a SKU, whoever wrote it: a second row for the same SKU is
-- two answers to a question that has one
create unique index if not exists magic_routes_sku_idx
  on public.magic_routes(lower(match)) where kind = 'sku';
create index if not exists magic_routes_order_idx
  on public.magic_routes(kind, sort_order);

alter table public.magic_routes enable row level security;


-- ---------- PART 2 of 4 : shared with the team ----------
-- The same decision as the batches themselves: everyone with an account
-- works the same morning, and can_print is what decides who is shown the
-- tile at all.
drop policy if exists "team_select" on public.magic_routes;
drop policy if exists "team_insert" on public.magic_routes;
drop policy if exists "team_update" on public.magic_routes;
drop policy if exists "team_delete" on public.magic_routes;

create policy "team_select" on public.magic_routes
  for select using (public.on_team(auth.uid()));
create policy "team_insert" on public.magic_routes
  for insert with check (public.on_team(auth.uid()) and auth.uid() = user_id);
create policy "team_update" on public.magic_routes
  for update using (public.on_team(auth.uid()));
create policy "team_delete" on public.magic_routes
  for delete using (public.on_team(auth.uid()));


-- ---------- PART 3 of 4 : live updates between devices ----------
-- A rule written on the office machine has to reach the one in the packing
-- room before the next Add all, or the two of them lay out different sheets.
alter table public.magic_routes replica identity full;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'magic_routes')
  then
    alter publication supabase_realtime add table public.magic_routes;
  end if;
end $$;


-- ---------- PART 4 of 4 : why the rule is there ----------
-- A rule read six months later is a piece of text and a destination, and
-- nothing about the morning it was written for. Remarks is that morning: the
-- shop it is for, the listing that changed, the person to ask. Added
-- separately so a routing table set up before this file grew keeps its rules
-- and simply gains the column.
alter table public.magic_routes
  add column if not exists remarks text not null default '';
