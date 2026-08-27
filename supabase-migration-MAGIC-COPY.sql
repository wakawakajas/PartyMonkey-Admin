-- ============================================================
-- MAGIC CREATE — COPY FILE
-- Run in the Supabase SQL Editor AFTER supabase-migration-MAGIC-ROUTES.sql.
-- Safe to run more than once, and safe to run as one batch.
--
-- Some designs need no template and no editing at all. The whole job is to
-- take one file out of the designs folder and put it in the folder the print
-- room works from — and that line is then finished. It was being done by hand
-- in File Explorer, once per SKU, by whoever was at the machine.
--
-- So it becomes a destination like the sheets are: 'copy'. The file it copies
-- and the folder it lands in are written on the route, which is shared — a
-- colleague opening the same batch copies the same file without mapping
-- anything again. The folder the files are copied INTO is the one thing that
-- cannot live here: a browser is given a folder by the person sitting at it
-- and holds it as a handle, not a path, so each machine chooses that once.
-- ============================================================

-- ---------- PART 1 of 2 : 'copy' is a destination ----------
alter table public.magic_routes drop constraint if exists magic_routes_dest_check;
alter table public.magic_routes
  add constraint magic_routes_dest_check
  check (dest in ('print','festive','custom','copy','skip'));


-- ---------- PART 2 of 2 : which file, and where it lands ----------
-- copy_path  the file, as a path under the designs folder — "Big TY Card/AAA.png".
--            Blank on a copy row means "work it out from the SKU's own name",
--            which is what happened before anything was mapped.
-- copy_to    a folder inside the chosen copy-to folder, made if it is not
--            there. Blank puts the file in the copy-to folder itself.
alter table public.magic_routes
  add column if not exists copy_path text not null default '';
alter table public.magic_routes
  add column if not exists copy_to text not null default '';
