-- ============================================================
-- What is taking up the space
-- Paste into the Supabase SQL Editor and run. Read-only: it deletes nothing.
-- Run the blocks one at a time -- the editor shows the last result only.
-- ============================================================

-- ---------- 1. the two totals, side by side ----------
-- "Database" on the dashboard is this first number. Storage (the buckets) is
-- billed and shown separately, and is usually the one that grows.
select 'database' as what,
       pg_size_pretty(pg_database_size(current_database())) as size
union all
select 'storage (all buckets)',
       pg_size_pretty(coalesce(sum((metadata->>'size')::bigint),0))
  from storage.objects;


-- ---------- 2. storage, by bucket ----------
select bucket_id,
       count(*) as files,
       pg_size_pretty(sum((metadata->>'size')::bigint)) as size,
       pg_size_pretty(avg((metadata->>'size')::bigint)::bigint) as avg_file
  from storage.objects
 group by bucket_id
 order by sum((metadata->>'size')::bigint) desc;


-- ---------- 3. storage, by what it is ----------
-- Everything the app uploads goes to shipment-photos under <user id>/<thing>/,
-- so the second path segment says which screen put it there.
select case
         when path_tokens[1] = 'adhoc' then 'adhoc/' || coalesce(path_tokens[2],'')
         when path_tokens[2] is null then '(root)'
         -- box-<id>, pickup-<id>, announce-<id>, request-<id> -> the word
         else split_part(path_tokens[2],'-',1)
       end as kind,
       count(*) as files,
       pg_size_pretty(sum((metadata->>'size')::bigint)) as size
  from storage.objects
 where bucket_id = 'shipment-photos'
 group by 1
 order by sum((metadata->>'size')::bigint) desc;


-- ---------- 4. the fattest single files ----------
select bucket_id, name,
       pg_size_pretty((metadata->>'size')::bigint) as size,
       created_at
  from storage.objects
 order by (metadata->>'size')::bigint desc nulls last
 limit 30;


-- ---------- 5. the biggest tables, indexes and all ----------
select c.relname as table_name,
       pg_size_pretty(pg_total_relation_size(c.oid)) as total,
       pg_size_pretty(pg_relation_size(c.oid))       as rows_only,
       pg_size_pretty(pg_total_relation_size(c.oid) - pg_relation_size(c.oid)) as indexes_toast,
       (select reltuples::bigint from pg_class where oid = c.oid) as approx_rows
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where c.relkind = 'r'
   and n.nspname in ('public','storage','auth')
 order by pg_total_relation_size(c.oid) desc
 limit 25;


-- ---------- 6. files nothing points at any more ----------
-- The app already knows this question: orphan_photos() anti-joins the bucket
-- against every path column in the schema, and the drawer's gauge reads the
-- count. A row deleted without its file leaves the file behind, and that is
-- the usual reason storage creeps up on its own.
--
-- NOTE: both of these guard on on_team(auth.uid()), and the SQL Editor runs
-- with no signed-in user, so they come back empty there. Read the number off
-- the gauge in the app's menu instead, or run them from the app.
select files,
       pg_size_pretty(bytes) as wasted
  from public.orphan_photo_usage();

-- and which ones, largest first
select path, pg_size_pretty(bytes) as size
  from public.orphan_photos()
 order by bytes desc
 limit 30;
