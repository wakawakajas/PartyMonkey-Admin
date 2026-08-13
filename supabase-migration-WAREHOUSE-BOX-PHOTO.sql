-- ============================================================
-- SEARCH WAREHOUSE: THE BOX, AND ITS PHOTO
-- Run in the Supabase SQL Editor AFTER supabase-migration-WAREHOUSE-SEARCH.sql.
-- Safe to run more than once, and safe to run as one batch.
--
-- Searching finds a line, but what somebody standing in the warehouse needs is
-- the box it is in, and what that box looks like. So the search now carries
-- the boxes a line landed in, and their photos, and the photo is the one thing
-- this screen may change — a box gets repacked and its picture goes stale.
--
-- The SKU photo becomes read-only here. It describes the goods rather than
-- where they are, and the order screen is where it belongs.
-- ============================================================

-- ---------- PART 1 of 4 : the search, now carrying the boxes ----------
-- Boxes come back as one jsonb array per line rather than as extra rows,
-- because a line split across three boxes is still one line — repeating it
-- three times would read as three separate findings of the same thing.
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
  description text,
  boxes       jsonb
)
language sql
security definer
stable
set search_path = public
as $func$
  select i.id, i.sku, coalesce(i.qty,0), i.remarks, i.photo_path, i.photo_url,
         -- never more than was ordered, the same ceiling the order list applies
         least(coalesce(a.arrived,0), coalesce(i.qty,0))::integer,
         s.order_id, s.description,
         coalesce(bx.boxes, '[]'::jsonb)
    from public.shipment_items i
    join public.shipments s on s.id = i.shipment_id
    left join (select item_id, sum(qty)::integer as arrived
                 from public.shipment_box_items
                group by item_id) a on a.item_id = i.id
    left join (
      -- one entry per box this line actually has pieces in, newest box last
      select bi.item_id,
             jsonb_agg(jsonb_build_object(
               'id',    b.id,
               'label', b.label,
               'qty',   bi.qty,
               'photo_path', b.photo_path
             ) order by b.created_at) as boxes
        from public.shipment_box_items bi
        join public.shipment_boxes b on b.id = bi.box_id
       where coalesce(bi.qty,0) > 0
       group by bi.item_id
    ) bx on bx.item_id = i.id
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


-- ---------- PART 2 of 4 : changing a box's photo ----------
-- The only thing this screen may write now. Nothing else about the box —
-- its size, its rate, which order it belongs to — is reachable from here.
create or replace function public.set_box_photo(box uuid, path text)
returns void
language sql
security definer
set search_path = public
as $func$
  update public.shipment_boxes
     set photo_path = path
   where id = box and public.on_team(auth.uid());
$func$;

revoke all on function public.set_box_photo(uuid, text) from public;
grant execute on function public.set_box_photo(uuid, text) to authenticated;


-- ---------- PART 3 of 4 : seeing and replacing the picture ----------
-- Box photos sit at <uid>/box-<box-id>/<file>, in the folder of whoever took
-- them, so the bucket's "your own folder only" rule hides them from everybody
-- else — the same problem item photos had. This says: the team may read a file
-- belonging to a box, and only that.
create or replace function public.is_box_photo(p text)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select exists (
    select 1 from public.shipment_boxes b
     where 'box-' || b.id::text = (storage.foldername(p))[2]
  );
$func$;

drop policy if exists "box_photos_team_select" on storage.objects;
drop policy if exists "box_photos_team_delete" on storage.objects;

create policy "box_photos_team_select" on storage.objects
  for select using (
    bucket_id = 'shipment-photos'
    and public.on_team(auth.uid())
    and public.is_box_photo(name)
  );

-- Replacing a photo means removing the one that was there, and it will usually
-- belong to somebody else — whoever packed the box. Without this, replacing it
-- would work but leave the old file behind for good.
create policy "box_photos_team_delete" on storage.objects
  for delete using (
    bucket_id = 'shipment-photos'
    and public.on_team(auth.uid())
    and public.is_box_photo(name)
  );

-- Uploading needs no new rule: a photo goes into the uploader's own folder,
-- which the bucket already allows.


-- ---------- PART 4 of 4 : check ----------
-- Should return one row per line, with boxes as a jsonb array:
--   select sku, order_id, boxes from public.search_procurement('a') limit 5;
