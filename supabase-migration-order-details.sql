-- Migration: order detail (items, parcel boxes, tracking).
-- Run ONCE in the Supabase SQL Editor. Clear the box first (Ctrl+A, Delete),
-- paste this whole file, then Run. Expect "Success. No rows returned".

-- one FX rate per order, shared by the items list and the box screens
alter table public.shipments add column if not exists rmb_rate numeric not null default 5.2;

-- boxes first: shipment_items references them
create table if not exists public.shipment_boxes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  shipment_id uuid not null references public.shipments(id) on delete cascade,
  label text,
  length_cm numeric not null default 0,
  breadth_cm numeric not null default 0,
  height_cm numeric not null default 0,
  cbm_rate_rmb numeric not null default 0,
  created_at timestamptz not null default now()
);

create table if not exists public.shipment_items (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  shipment_id uuid not null references public.shipments(id) on delete cascade,
  -- which box this SKU arrived in; null = not yet arrived. on delete set null
  -- so deleting a box un-assigns its items instead of destroying them.
  box_id uuid references public.shipment_boxes(id) on delete set null,
  sku text not null default '',
  qty integer not null default 1,
  unit_price_rmb numeric not null default 0,
  sort_order integer not null default 0,
  created_at timestamptz not null default now()
);

create table if not exists public.shipment_tracking (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  shipment_id uuid not null references public.shipments(id) on delete cascade,
  tracking_number text not null,
  eta date,
  created_at timestamptz not null default now()
);

alter table public.shipment_boxes    enable row level security;
alter table public.shipment_items    enable row level security;
alter table public.shipment_tracking enable row level security;

-- same per-user isolation as templates/shipments
do $$
declare t text;
begin
  foreach t in array array['shipment_boxes','shipment_items','shipment_tracking'] loop
    execute format('drop policy if exists "own_select" on public.%I', t);
    execute format('drop policy if exists "own_insert" on public.%I', t);
    execute format('drop policy if exists "own_update" on public.%I', t);
    execute format('drop policy if exists "own_delete" on public.%I', t);
    execute format('create policy "own_select" on public.%I for select using (auth.uid() = user_id)', t);
    execute format('create policy "own_insert" on public.%I for insert with check (auth.uid() = user_id)', t);
    execute format('create policy "own_update" on public.%I for update using (auth.uid() = user_id)', t);
    execute format('create policy "own_delete" on public.%I for delete using (auth.uid() = user_id)', t);
  end loop;
end $$;

create index if not exists shipment_items_shipment_idx    on public.shipment_items(shipment_id);
create index if not exists shipment_boxes_shipment_idx    on public.shipment_boxes(shipment_id);
create index if not exists shipment_tracking_shipment_idx on public.shipment_tracking(shipment_id);
