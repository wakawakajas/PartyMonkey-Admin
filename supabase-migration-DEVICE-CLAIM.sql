-- ============================================================
-- LET A DEVICE BE CLAIMED BY WHOEVER SIGNS IN ON IT
-- Run in the Supabase SQL Editor. Safe to run more than once.
--
-- A push subscription belongs to a browser, not a person. When a second
-- account signs in on the same phone, the app re-registers that device to
-- them — but the old rule only let you update rows you already owned, so the
-- claim failed and the phone kept receiving the previous account's
-- notifications.
--
-- The check clause is what keeps this safe: you may only ever point a device
-- at yourself, never at somebody else.
-- ============================================================

drop policy if exists "own_update"    on public.push_subscriptions;
drop policy if exists "claim_device"  on public.push_subscriptions;

create policy "claim_device" on public.push_subscriptions
  for update using (true) with check (auth.uid() = user_id);
