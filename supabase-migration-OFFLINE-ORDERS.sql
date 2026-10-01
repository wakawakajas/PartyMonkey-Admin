-- ============================================================
-- FINANCE — OFFLINE ORDERS
-- Run AFTER supabase-migration-FINANCE-COSTS.sql (uses on_finance_team).
-- Safe to run more than once.
--
-- One row per invoice. The invoice PDF itself is never kept: the app reads
-- the text off it (number, dates, client, items, total) and only that is
-- saved. The rest is the shop's own tracking of the order.
-- ============================================================

create table if not exists public.offline_orders (
  id uuid primary key default gen_random_uuid(),
  invoice_no text not null unique,
  invoice_date date,
  due_date date,
  client_name text,
  client_contact text,
  delivery_address text,
  client_remarks text,
  -- [{"desc": "Printing of Namecard\nGlossy 250gsm\nSingle Side Print", "qty": 1, "unit": 10, "amount": 10}]
  items jsonb not null default '[]'::jsonb,
  fulfilment text,                 -- "Self Collection", "Delivery", … as the invoice says
  total numeric,
  paid boolean not null default false,
  done boolean not null default false,
  sent boolean not null default false,   -- collected or sent
  -- [{"paper": 3, "name": "Cavallo Plus (Gloss) 250", "sheets": 10, "side": "single"}]
  pos_items jsonb not null default '[]'::jsonb,
  pos_price numeric,
  tracking text,
  our_remarks text,
  created_at timestamptz not null default now(),
  created_by uuid references auth.users(id) default auth.uid(),
  updated_at timestamptz not null default now()
);

alter table public.offline_orders enable row level security;

drop policy if exists "offline_orders_all" on public.offline_orders;
create policy "offline_orders_all" on public.offline_orders
  for all using (public.on_finance_team(auth.uid()) or public.is_admin(auth.uid()))
  with check (public.on_finance_team(auth.uid()) or public.is_admin(auth.uid()));

-- check:
--   select invoice_no, client_contact, total from public.offline_orders order by created_at desc;
