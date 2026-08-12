-- ============================================================
-- SEARCH WAREHOUSE FOR PEOPLE WITHOUT PROCUREMENT ACCESS
-- Run in the Supabase SQL Editor AFTER supabase-migration-ADHOC.sql,
-- which is where public.on_team comes from. Safe to run more than once.
--
-- Orders belong to the account that entered them and that does not change:
-- nothing here opens up the shipment tables, so Procurement & Shipping keeps
-- showing each person only their own. What opens up is the single question
-- Search Warehouse asks. It is answered by a function running with the owner's
-- rights that hands back only the fields that screen puts on a card, so
-- somebody with no access to Procurement can look a thing up without being
-- able to read prices, fees, or anything else about the order it came in on.
-- ============================================================


-- ---------- PART 1 of 3 : the search ----------
drop function if exists public.search_procurement(text);
create function public.search_procurement(q text)
returns table(
  item_id     uuid,
  sku         text,
  qty         integer,
  remarks     text,
  photo_path  text,
  photo_url   text,
  arrived     integer,
  order_id    text,
  description text
)
language sql
security definer
stable
set search_path = public
as $func$
  select i.id, i.sku, coalesce(i.qty,0), i.remarks, i.photo_path, i.photo_url,
         -- never more than was ordered, the same ceiling the order list applies
         least(coalesce(a.arrived,0), coalesce(i.qty,0))::integer,
         s.order_id, s.description
    from public.shipment_items i
    join public.shipments s on s.id = i.shipment_id
    left join (select item_id, sum(qty)::integer as arrived
                 from public.shipment_box_items
                group by item_id) a on a.item_id = i.id
   where public.on_team(auth.uid())      -- signed in and on the team, or nothing
     and s.deleted_at is null            -- an order in the bin is not findable
     and length(coalesce(q,'')) >= 2
     -- the words are on the line itself, or on the order carrying it, which is
     -- what makes searching a supplier's description return that order's lines
     and (i.sku ilike '%'||q||'%' or i.remarks ilike '%'||q||'%'
          or s.order_id ilike '%'||q||'%' or s.description ilike '%'||q||'%')
   order by s.created_at desc nulls last, i.sort_order
   limit 60;
$func$;

revoke all on function public.search_procurement(text) from public;
grant execute on function public.search_procurement(text) to authenticated;


-- ---------- PART 2 of 3 : changing a line's photo ----------
-- The one thing this screen may write. Nothing else about the line is reachable.
drop function if exists public.set_item_photo(uuid, text);
create function public.set_item_photo(item uuid, path text)
returns void
language sql
security definer
set search_path = public
as $func$
  update public.shipment_items
     set photo_path = path, photo_url = null
   where id = item and public.on_team(auth.uid());
$func$;

revoke all on function public.set_item_photo(uuid, text) from public;
grant execute on function public.set_item_photo(uuid, text) to authenticated;


-- ---------- PART 3 of 3 : seeing the picture ----------
-- A photo sits in the folder of whoever uploaded it, so the "your own folder
-- only" rule on the bucket hides it from the rest of the team — the same
-- problem SKU photos had. The team may read a file that belongs to an order
-- line, and only that. The check runs with the owner's rights because the
-- table it looks in is itself private.
create or replace function public.is_item_photo(p text)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select exists (
    select 1 from public.shipment_items i
     where i.id::text = (storage.foldername(p))[2]
  );
$func$;

drop policy if exists "item_photos_team_select" on storage.objects;
create policy "item_photos_team_select" on storage.objects
  for select using (
    bucket_id = 'shipment-photos'
    and public.on_team(auth.uid())
    and public.is_item_photo(name)
  );
