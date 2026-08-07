-- ============================================================
-- COMBINED MIGRATION — run this ONE file and you are up to date.
-- Safe to run even if you ran an earlier version of it; every
-- statement checks before it changes anything.
--
-- HOW TO RUN
--   1. Supabase dashboard -> SQL Editor -> New query
--   2. Click in the box, Ctrl+A then Delete (make sure it is EMPTY)
--   3. Paste this whole file, click Run
--   4. Expect: "Success. No rows returned"
-- ============================================================

-- ---- columns on shipments ----
alter table public.shipments add column if not exists china_fee_rmb numeric not null default 0;
alter table public.shipments add column if not exists china_basis  text    not null default 'ordered';
alter table public.shipments add column if not exists freight_mode text    not null default 'box';

alter table public.shipments drop constraint if exists shipments_china_basis_chk;
alter table public.shipments add  constraint shipments_china_basis_chk
  check (china_basis in ('ordered','arrived'));

alter table public.shipments drop constraint if exists shipments_freight_mode_chk;
alter table public.shipments add  constraint shipments_freight_mode_chk
  check (freight_mode in ('box','shared'));

-- ---- item photos + follow-up remarks ----
alter table public.shipment_items add column if not exists photo_path text;
alter table public.shipment_items add column if not exists remarks    text;

insert into storage.buckets (id, name, public)
values ('shipment-photos', 'shipment-photos', false)
on conflict (id) do nothing;

drop policy if exists "shipment_photos_select" on storage.objects;
drop policy if exists "shipment_photos_insert" on storage.objects;
drop policy if exists "shipment_photos_update" on storage.objects;
drop policy if exists "shipment_photos_delete" on storage.objects;

create policy "shipment_photos_select" on storage.objects for select
  using (bucket_id = 'shipment-photos' and (storage.foldername(name))[1] = auth.uid()::text);
create policy "shipment_photos_insert" on storage.objects for insert
  with check (bucket_id = 'shipment-photos' and (storage.foldername(name))[1] = auth.uid()::text);
create policy "shipment_photos_update" on storage.objects for update
  using (bucket_id = 'shipment-photos' and (storage.foldername(name))[1] = auth.uid()::text);
create policy "shipment_photos_delete" on storage.objects for delete
  using (bucket_id = 'shipment-photos' and (storage.foldername(name))[1] = auth.uid()::text);

-- ---- partial arrivals -------------------------------------------------
-- An item can now arrive across several boxes, so how many pieces landed
-- in which box moves out of shipment_items.box_id into its own table.
create table if not exists public.shipment_box_items (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  shipment_id uuid not null references public.shipments(id) on delete cascade,
  box_id uuid not null references public.shipment_boxes(id) on delete cascade,
  item_id uuid not null references public.shipment_items(id) on delete cascade,
  qty integer not null default 0,
  created_at timestamptz not null default now(),
  unique (box_id, item_id)
);

alter table public.shipment_box_items enable row level security;

do $$
begin
  execute 'drop policy if exists "own_select" on public.shipment_box_items';
  execute 'drop policy if exists "own_insert" on public.shipment_box_items';
  execute 'drop policy if exists "own_update" on public.shipment_box_items';
  execute 'drop policy if exists "own_delete" on public.shipment_box_items';
  execute 'create policy "own_select" on public.shipment_box_items for select using (auth.uid() = user_id)';
  execute 'create policy "own_insert" on public.shipment_box_items for insert with check (auth.uid() = user_id)';
  execute 'create policy "own_update" on public.shipment_box_items for update using (auth.uid() = user_id)';
  execute 'create policy "own_delete" on public.shipment_box_items for delete using (auth.uid() = user_id)';
end $$;

-- carry any existing whole-item assignments over, then retire the old column
do $$
begin
  if exists (select 1 from information_schema.columns
             where table_schema='public' and table_name='shipment_items' and column_name='box_id') then
    insert into public.shipment_box_items (user_id, shipment_id, box_id, item_id, qty)
    select user_id, shipment_id, box_id, id, qty
      from public.shipment_items
     where box_id is not null
    on conflict (box_id, item_id) do nothing;

    alter table public.shipment_items drop column box_id;
  end if;
end $$;

create index if not exists sbi_shipment_idx on public.shipment_box_items(shipment_id);
create index if not exists sbi_item_idx     on public.shipment_box_items(item_id);
create index if not exists sbi_box_idx      on public.shipment_box_items(box_id);

-- ---- consolidated shipments -------------------------------------------
create table if not exists public.consolidations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  freight_number text not null,
  eta date,
  created_at timestamptz not null default now()
);

alter table public.consolidations enable row level security;

do $$
begin
  execute 'drop policy if exists "own_select" on public.consolidations';
  execute 'drop policy if exists "own_insert" on public.consolidations';
  execute 'drop policy if exists "own_update" on public.consolidations';
  execute 'drop policy if exists "own_delete" on public.consolidations';
  execute 'create policy "own_select" on public.consolidations for select using (auth.uid() = user_id)';
  execute 'create policy "own_insert" on public.consolidations for insert with check (auth.uid() = user_id)';
  execute 'create policy "own_update" on public.consolidations for update using (auth.uid() = user_id)';
  execute 'create policy "own_delete" on public.consolidations for delete using (auth.uid() = user_id)';
end $$;

-- a single nullable FK is what enforces "one order in at most one freight number"
alter table public.shipments
  add column if not exists consolidation_id uuid references public.consolidations(id) on delete set null;

create index if not exists shipments_consolidation_idx on public.shipments(consolidation_id);

-- ---- manual ordering for order IDs and freight numbers ----------------
alter table public.shipments     add column if not exists sort_order integer not null default 0;
alter table public.consolidations add column if not exists sort_order integer not null default 0;

-- backfill newest-first so the existing on-screen order is preserved
update public.shipments s set sort_order = sub.rn
from (select id, row_number() over (partition by user_id order by created_at desc) - 1 as rn
      from public.shipments) sub
where s.id = sub.id and s.sort_order = 0;

update public.consolidations c set sort_order = sub.rn
from (select id, row_number() over (partition by user_id order by created_at desc) - 1 as rn
      from public.consolidations) sub
where c.id = sub.id and c.sort_order = 0;
