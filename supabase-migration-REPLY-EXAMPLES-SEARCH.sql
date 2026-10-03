-- The reply-draft function used to download the newest 1,000 saved replies for
-- every draft and rank them itself. This ranks inside Supabase and hands back
-- only the ~100 that could matter, so the table can keep growing forever (the
-- shop learns from every chat) without the egress growing with it.
--
-- security invoker: row level security applies as the signed-in caller, exactly
-- as it does when the function reads the table directly.
-- Safe to run twice.
create or replace function public.reply_examples_search(
  p_words    text[],
  p_chat_key text default '',
  p_limit    integer default 60
)
returns table (
  id uuid, buyer_text text, reply_text text, hits integer, edited boolean,
  created_at timestamptz, chat_key text, buyer text
)
language sql
stable
security invoker
set search_path = public
as $$
  with scored as (
    select e.id, e.buyer_text, e.reply_text, e.hits, e.edited, e.created_at,
           e.chat_key, e.buyer,
           (select count(*)
              from unnest(coalesce(p_words, '{}'::text[])) w
             where position(w in lower(coalesce(e.buyer_text, '') || ' ' || coalesce(e.reply_text, ''))) > 0
           ) as s
      from public.reply_examples e
  ),
  by_words as (
    select * from scored where s > 0
     order by s desc, hits desc, created_at desc
     limit greatest(1, least(coalesce(p_limit, 60), 200))
  ),
  by_chat as (
    select * from scored
     where coalesce(p_chat_key, '') <> '' and chat_key = p_chat_key
     order by created_at desc
     limit 20
  ),
  by_hits as (
    select * from scored order by hits desc, created_at desc limit 8
  ),
  every as (
    select * from by_words
    union select * from by_chat
    union select * from by_hits
  )
  select id, buyer_text, reply_text, hits, edited, created_at, chat_key, buyer
    from every;
$$;

grant execute on function public.reply_examples_search(text[], text, integer) to authenticated;
