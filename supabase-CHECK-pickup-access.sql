-- Paste into the Supabase SQL Editor and Run. Returns a short report; nothing
-- is changed. Send the output back and it says exactly where this is stuck.
select 1 as ord, 'ACCOUNT' as what,
       coalesce(email,user_id::text) as detail,
       concat('admin=',is_admin,'  pickup=',can_pickup,
              '  shipping=',can_shipping,'  print=',can_print) as value
from public.profiles

union all
select 2, 'ACCOUNT WITH NO PROFILE ROW', u.email, 'never granted anything'
from auth.users u
left join public.profiles p on p.user_id = u.id
where p.user_id is null

union all
select 3, 'POLICY ON pickups', policyname, cmd::text
from pg_policies where schemaname='public' and tablename='pickups'

union all
select 4, 'HELPER FUNCTION', p.proname, 'exists'
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where n.nspname='public' and p.proname in ('on_pickup_team','is_admin')

union all
select 5, 'PICK UPS OWNED BY', coalesce(u.email,'(deleted account)'), count(*)::text
from public.pickups pk
left join auth.users u on u.id = pk.user_id
group by u.email

union all
select 6, 'DEVICES REGISTERED FOR PUSH', coalesce(u.email,'(unknown)'), count(*)::text
from public.push_subscriptions s
left join auth.users u on u.id = s.user_id
group by u.email

order by ord, detail;
