-- ============================================================
-- FORCE SIGN OUT — run in the Supabase SQL Editor.
-- Safe to run more than once. Needs the ACCESS migration first.
--
-- The GoTrue admin API has no "log this user out" endpoint, so the session is
-- ended where it actually lives: the auth tables. Only Postgres can reach
-- those, hence a security definer function rather than an edge function.
-- ============================================================

create or replace function public.force_sign_out(target uuid)
returns integer
language plpgsql
security definer
set search_path = public
as $func$
declare
  devices integer := 0;
begin
  -- the caller's admin flag is read from the database, never trusted from the app
  if not public.is_admin(auth.uid()) then
    raise exception 'only an admin can sign other people out';
  end if;
  if target = auth.uid() then
    raise exception 'use Sign out in the menu to sign yourself out';
  end if;

  delete from public.push_subscriptions where user_id = target;
  get diagnostics devices = row_count;

  -- newer Supabase keeps sessions here; deleting them ends every login
  begin
    delete from auth.sessions where user_id = target;
  exception when undefined_table or undefined_column then
    null;   -- older project without a sessions table
  end;

  -- and revoke the refresh tokens, so nothing can be renewed
  begin
    update auth.refresh_tokens set revoked = true where user_id = target::text;
  exception when undefined_table or undefined_column or datatype_mismatch then
    null;
  end;

  return devices;
end $func$;

revoke all on function public.force_sign_out(uuid) from public;
grant execute on function public.force_sign_out(uuid) to authenticated;
