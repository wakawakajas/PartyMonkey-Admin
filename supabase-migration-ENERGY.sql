-- ============================================================
-- TEAM ENERGY METER
-- Run in the Supabase SQL Editor AFTER supabase-migration-ADHOC.sql,
-- which is where public.on_team comes from. Safe to run more than once.
--
-- One question a day, four answers, and a mix nobody can trace back to a
-- person. That last part is the whole feature: if an answer could be pinned to
-- a name, the honest answer stops being given. So the rows themselves are
-- readable only by whoever wrote them, and the team sees the tally through a
-- function that counts without ever handing over who.
--
-- The day is a plain date, worked out in Singapore time by the app before it
-- gets here. Nothing on the server has to know which timezone the shop is in.
-- ============================================================


-- ---------- PART 1 of 5 : who may edit the questions ----------
-- profiles is itself locked down, so the check has to run with the owner's
-- rights rather than the asker's
create or replace function public.is_app_admin(uid uuid)
returns boolean
language sql
security definer
stable
set search_path = public
as $func$
  select exists (select 1 from public.profiles p where p.user_id = uid and p.is_admin);
$func$;


-- ---------- PART 2 of 5 : the question sets ----------
create table if not exists public.energy_sets (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  question text not null,
  -- exactly four, in order: [{"ico":"🎯","name":"Deep Focus","hint":"…"}, …]
  -- The position carries the meaning — first is the most capacity, last the
  -- least — which is what lets days with different wording still be compared.
  modes jsonb not null,
  created_at timestamptz not null default now()
);

alter table public.energy_sets drop constraint if exists energy_sets_four_modes;
alter table public.energy_sets add constraint energy_sets_four_modes
  check (jsonb_typeof(modes) = 'array' and jsonb_array_length(modes) = 4);

-- the app orders sets by this, and the day's draw walks that same order, so it
-- has to be stable for everyone
create index if not exists energy_sets_created_idx on public.energy_sets(created_at);

alter table public.energy_sets enable row level security;

drop policy if exists "energy_sets_read"   on public.energy_sets;
drop policy if exists "energy_sets_insert" on public.energy_sets;
drop policy if exists "energy_sets_update" on public.energy_sets;
drop policy if exists "energy_sets_delete" on public.energy_sets;

-- everyone on the team needs to read them: it is what they are asked
create policy "energy_sets_read" on public.energy_sets
  for select using (public.on_team(auth.uid()));
-- but only an admin writes them
create policy "energy_sets_insert" on public.energy_sets
  for insert with check (public.is_app_admin(auth.uid()) and auth.uid() = user_id);
create policy "energy_sets_update" on public.energy_sets
  for update using (public.is_app_admin(auth.uid()));
create policy "energy_sets_delete" on public.energy_sets
  for delete using (public.is_app_admin(auth.uid()));


-- ---------- PART 3 of 5 : the answers ----------
create table if not exists public.energy_signals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  day date not null,
  slot smallint not null check (slot between 0 and 3),
  -- which set was being asked, kept so an old day still reads correctly after
  -- the wording is edited or the set is deleted
  set_id uuid references public.energy_sets(id) on delete set null,
  created_at timestamptz not null default now()
);

-- one answer per person per day; changing your mind updates it rather than
-- stacking a second one
create unique index if not exists energy_signals_one_per_day
  on public.energy_signals(user_id, day);
create index if not exists energy_signals_day_idx on public.energy_signals(day);

alter table public.energy_signals enable row level security;

drop policy if exists "energy_signals_own_select" on public.energy_signals;
drop policy if exists "energy_signals_own_insert" on public.energy_signals;
drop policy if exists "energy_signals_own_update" on public.energy_signals;
drop policy if exists "energy_signals_own_delete" on public.energy_signals;

-- Own row only, and deliberately no team read. Anyone can see their own answer
-- and nobody else's; the tally comes from the function below instead. This is
-- what makes "anonymous" true rather than merely promised in the interface.
create policy "energy_signals_own_select" on public.energy_signals
  for select using (auth.uid() = user_id);
create policy "energy_signals_own_insert" on public.energy_signals
  for insert with check (auth.uid() = user_id and public.on_team(auth.uid()));
create policy "energy_signals_own_update" on public.energy_signals
  for update using (auth.uid() = user_id);
create policy "energy_signals_own_delete" on public.energy_signals
  for delete using (auth.uid() = user_id);


-- ---------- PART 4 of 5 : the mix, counted but not attributed ----------
-- Returns how many chose each slot, and nothing else — no ids, no names, no
-- times. Written this way round so that even someone poking at the API gets
-- counts rather than people.
create or replace function public.energy_mix(d date)
returns table(slot smallint, n integer)
language sql
security definer
stable
set search_path = public
as $func$
  select s.slot, count(*)::integer
    from public.energy_signals s
   where public.on_team(auth.uid())
     and s.day = d
   group by s.slot
   order by s.slot;
$func$;

revoke all on function public.energy_mix(date) from public;
grant execute on function public.energy_mix(date) to authenticated;

-- how many people could have answered, for the "8 of 15" line
create or replace function public.energy_team_size()
returns integer
language sql
security definer
stable
set search_path = public
as $func$
  select case when public.on_team(auth.uid())
              then (select count(*)::integer from public.profiles)
              else 0 end;
$func$;

revoke all on function public.energy_team_size() from public;
grant execute on function public.energy_team_size() to authenticated;


-- ---------- PART 5 of 5 : something to ask on day one ----------
-- Only if the table is empty, so re-running this file never duplicates them
-- and never overwrites wording an admin has since changed.
insert into public.energy_sets (user_id, name, question, modes)
select p.user_id, v.name, v.question, v.modes
  from (select user_id from public.profiles where is_admin order by user_id limit 1) p
 cross join (values
   ('Working style','How are you working today?', '[
      {"ico":"🎯","name":"Deep Focus","hint":"Head down — ping me later"},
      {"ico":"💬","name":"Open","hint":"Interrupt me, it is fine"},
      {"ico":"📦","name":"On the floor","hint":"Picking or packing"},
      {"ico":"🪫","name":"Running low","hint":"At capacity — go easy"}]'::jsonb),
   ('Weather','What is your weather today?', '[
      {"ico":"☀️","name":"Sunny","hint":"Flying, send me things"},
      {"ico":"⛅","name":"Bright","hint":"Good, with the odd cloud"},
      {"ico":"🌧️","name":"Rainy","hint":"Slow going today"},
      {"ico":"⛈️","name":"Stormy","hint":"Weathering it — go easy"}]'::jsonb),
   ('Fuel','How is the tank?', '[
      {"ico":"🚀","name":"Full","hint":"Give me the big job"},
      {"ico":"🔋","name":"Charged","hint":"Plenty left in it"},
      {"ico":"🥱","name":"Half","hint":"Ticking over"},
      {"ico":"🕯️","name":"Fumes","hint":"Nearly out"}]'::jsonb)
 ) as v(name,question,modes)
 where not exists (select 1 from public.energy_sets);
