-- Run this once in the Supabase SQL Editor (Project → SQL Editor → New query).
-- Creates the templates and shipments tables with row-level security so
-- each signed-in user can only ever see and modify their own data.

create table public.templates (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  data jsonb not null,
  sort_order integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, name)
);

alter table public.templates enable row level security;

create policy "Users can view own templates"
  on public.templates for select
  using (auth.uid() = user_id);

create policy "Users can insert own templates"
  on public.templates for insert
  with check (auth.uid() = user_id);

create policy "Users can update own templates"
  on public.templates for update
  using (auth.uid() = user_id);

create policy "Users can delete own templates"
  on public.templates for delete
  using (auth.uid() = user_id);

create table public.shipments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  order_id text not null,
  description text,
  rmb_rate numeric not null default 5.2,
  china_fee_rmb numeric not null default 0,
  china_basis text not null default 'ordered' check (china_basis in ('ordered','arrived')),
  freight_mode text not null default 'box' check (freight_mode in ('box','shared')),
  created_at timestamptz not null default now()
);

alter table public.shipments enable row level security;

create policy "Users can view own shipments"
  on public.shipments for select
  using (auth.uid() = user_id);

create policy "Users can insert own shipments"
  on public.shipments for insert
  with check (auth.uid() = user_id);

create policy "Users can update own shipments"
  on public.shipments for update
  using (auth.uid() = user_id);

create policy "Users can delete own shipments"
  on public.shipments for delete
  using (auth.uid() = user_id);

-- order detail: parcel boxes, items bought, tracking numbers
create table public.shipment_boxes (
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

create table public.shipment_items (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  shipment_id uuid not null references public.shipments(id) on delete cascade,
  sku text not null default '',
  qty integer not null default 1,
  unit_price_rmb numeric not null default 0,
  photo_path text,
  remarks text,
  sort_order integer not null default 0,
  created_at timestamptz not null default now()
);

-- how many pieces of an item landed in which box (an item can split across boxes)
create table public.shipment_box_items (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  shipment_id uuid not null references public.shipments(id) on delete cascade,
  box_id uuid not null references public.shipment_boxes(id) on delete cascade,
  item_id uuid not null references public.shipment_items(id) on delete cascade,
  qty integer not null default 0,
  created_at timestamptz not null default now(),
  unique (box_id, item_id)
);

create table public.consolidations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  freight_number text not null,
  eta date,
  created_at timestamptz not null default now()
);

-- single FK = an order can sit in at most one freight number
alter table public.shipments
  add column consolidation_id uuid references public.consolidations(id) on delete set null;

create table public.shipment_tracking (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  shipment_id uuid not null references public.shipments(id) on delete cascade,
  tracking_number text not null,
  eta date,
  created_at timestamptz not null default now()
);

-- orders a customer collects in person
create table public.pickups (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  order_id  text not null,
  name      text not null default '',
  remarks   text not null default '',
  prepared  boolean not null default false,
  collected boolean not null default false,
  prepared_at  timestamptz,
  collected_at timestamptz,
  sort_order integer not null default 0,
  created_at timestamptz not null default now()
);

-- keep the whole previous row so a live update can say what changed
alter table public.pickups replica identity full;
alter publication supabase_realtime add table public.pickups;

alter table public.shipment_boxes     enable row level security;
alter table public.shipment_items     enable row level security;
alter table public.shipment_tracking  enable row level security;
alter table public.shipment_box_items enable row level security;
alter table public.consolidations     enable row level security;
alter table public.pickups            enable row level security;

do $$
declare t text;
begin
  foreach t in array array['shipment_boxes','shipment_items','shipment_tracking',
                           'shipment_box_items','consolidations','pickups'] loop
    execute format('create policy "own_select" on public.%I for select using (auth.uid() = user_id)', t);
    execute format('create policy "own_insert" on public.%I for insert with check (auth.uid() = user_id)', t);
    execute format('create policy "own_update" on public.%I for update using (auth.uid() = user_id)', t);
    execute format('create policy "own_delete" on public.%I for delete using (auth.uid() = user_id)', t);
  end loop;
end $$;

create index shipment_items_shipment_idx    on public.shipment_items(shipment_id);
create index shipment_boxes_shipment_idx    on public.shipment_boxes(shipment_id);
create index shipment_tracking_shipment_idx on public.shipment_tracking(shipment_id);
