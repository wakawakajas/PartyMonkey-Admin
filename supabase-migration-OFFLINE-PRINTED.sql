-- Offline Order: a "Printed" tick beside Paid. Informational only — it does not
-- count towards an order being completed (that stays Paid + Done + Collected/sent).
alter table public.offline_orders add column if not exists printed boolean not null default false;
