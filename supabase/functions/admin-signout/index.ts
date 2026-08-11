// Ends another account's session and unregisters their devices.
//
// A browser cannot sign someone else out — revoking a session needs the admin
// API, which needs the service role key, which must never reach the page. So
// it happens here: the caller's own token proves who they are, their profile
// row proves they are an admin, and only then does the service role act.
//
// Needs no secrets of its own; SUPABASE_SERVICE_ROLE_KEY is provided to Edge
// Functions automatically.
import { createClient } from "jsr:@supabase/supabase-js@2";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });

  try {
    const authHeader = req.headers.get("Authorization") ?? "";
    if (!authHeader) return json({ error: "not signed in" }, 401);

    const url = Deno.env.get("SUPABASE_URL")!;
    const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

    const sb = createClient(url, Deno.env.get("SUPABASE_ANON_KEY")!, {
      global: { headers: { Authorization: authHeader } },
    });
    const { data: { user } } = await sb.auth.getUser();
    if (!user) return json({ error: "not signed in" }, 401);

    const admin = createClient(url, serviceKey);

    // the caller must be an admin — checked against the database, never
    // against anything the page sent us
    const { data: me, error: meErr } = await admin
      .from("profiles").select("is_admin").eq("user_id", user.id).maybeSingle();
    if (meErr) return json({ error: "could not check your access: " + meErr.message }, 500);
    if (!me?.is_admin) return json({ error: "only an admin can sign other people out" }, 403);

    const { user_id } = await req.json();
    if (!user_id) return json({ error: "no account given" }, 400);
    if (user_id === user.id) {
      return json({ error: "use Sign out in the menu to sign yourself out" }, 400);
    }

    // their phones stop receiving notifications straight away
    const { count } = await admin
      .from("push_subscriptions").delete({ count: "exact" }).eq("user_id", user_id);

    // revoke every refresh token they hold. supabase-js has no wrapper for
    // this, so it is the GoTrue admin endpoint directly.
    const res = await fetch(`${url}/auth/v1/admin/users/${user_id}/logout`, {
      method: "POST",
      headers: {
        apikey: serviceKey,
        Authorization: `Bearer ${serviceKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ scope: "global" }),
    });
    if (!res.ok && res.status !== 204) {
      const text = await res.text();
      return json({ error: `sign out refused (${res.status}): ${text.slice(0, 160)}` }, 500);
    }

    return json({ ok: true, devicesRemoved: count ?? 0 });
  } catch (e) {
    console.error(e);
    return json({ error: String(e) }, 500);
  }
});
