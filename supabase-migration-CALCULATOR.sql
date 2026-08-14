-- ============================================================
-- PRODUCT CALCULATOR
-- Run in the Supabase SQL Editor AFTER supabase-migration-ACCESS.sql.
-- Safe to run more than once, and safe to run as one batch.
--
-- Works out the profit on a product, and the profit if it goes into a
-- campaign. The five things that change per product — name, cost, selling
-- price, MPQ, weight per piece — are typed each time and not stored. The
-- rates are: they are the marketplace's terms, the same for every product,
-- and they change without warning. An admin edits them once here rather than
-- everyone editing a copy.
-- ============================================================

-- ---------- PART 1 of 3 : who may open it ----------
-- defaults to false, so nobody gains a section without being given it
alter table public.profiles
  add column if not exists can_calculator boolean not null default false;

create or replace function public.on_calculator_team(uid uuid)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select coalesce((select can_calculator from public.profiles where user_id = uid), false);
$func$;


-- ---------- PART 2 of 3 : the rates ----------
-- One row, ever. The id is fixed so there is nothing to look up and no way to
-- end up with two sets of terms disagreeing with each other.
create table if not exists public.calc_settings (
  id boolean primary key default true check (id),
  -- charged to the buyer on every order, on top of the goods
  customer_shipping_fee numeric not null default 1.99,
  -- taken off the subtotal
  commission_pct        numeric not null default 10.9,
  -- taken off the buyer payment, which includes the shipping fee
  transaction_pct       numeric not null default 3.27,
  cashback_pct          numeric not null default 5.45,
  free_shipping         numeric not null default 0,
  -- a campaign discounts the selling price and charges a different commission
  campaign_discount_pct numeric not null default 2,
  campaign_commission_pct numeric not null default 7.63,
  campaign_transaction_pct numeric not null default 3.27,
  campaign_cashback_pct numeric not null default 5.45,
  campaign_free_shipping numeric not null default 0,
  -- what the courier charges, by total weight. Kept as a list rather than
  -- columns so a band can be added or dropped without another migration:
  --   [{"upTo": 1, "cost": 2.3}, ...] — the first band whose upTo the weight
  --   does not exceed wins, and a weight past the last band has no price.
  weight_bands jsonb not null default '[
    {"upTo": 1,    "cost": 2.3},
    {"upTo": 4.9,  "cost": 3.21},
    {"upTo": 9.9,  "cost": 3.79},
    {"upTo": 19.9, "cost": 6.07},
    {"upTo": 30,   "cost": 11.2}
  ]'::jsonb,
  updated_at timestamptz not null default now(),
  updated_by uuid references auth.users(id)
);

-- the one row, created if it is not already there
insert into public.calc_settings(id) values (true) on conflict (id) do nothing;

alter table public.calc_settings enable row level security;

drop policy if exists "calc_read"  on public.calc_settings;
drop policy if exists "calc_write" on public.calc_settings;

-- anyone who can open the calculator needs the terms it calculates with
create policy "calc_read" on public.calc_settings
  for select using (public.on_calculator_team(auth.uid()) or public.is_admin(auth.uid()));
-- but only an admin sets them: they decide what everybody's numbers mean
create policy "calc_write" on public.calc_settings
  for update using (public.is_admin(auth.uid()));


-- ---------- PART 3 of 3 : check ----------
-- The rates as they stand:
--   select * from public.calc_settings;
--
-- Who can open it — grant it from Users & access, or here:
--   update public.profiles set can_calculator = true where email = 'someone@example.com';
