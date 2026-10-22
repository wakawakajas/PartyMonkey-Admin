# print-template-builder

## Supabase: every new table needs grants

From 2026-10-30 Supabase no longer grants Data API access to new tables in
`public` automatically. A table created without grants answers every
supabase-js call with "permission denied" (42501).

Any SQL file that runs `create table public.<name>` must grant it in the same
file, right after the table (and its RLS policies):

```sql
grant select on public.<name> to anon;
grant select, insert, update, delete on public.<name> to authenticated;
grant select, insert, update, delete on public.<name> to service_role;
```

Drop the `anon` line for tables only signed-in users should read. Existing
tables keep the grants they already have.
