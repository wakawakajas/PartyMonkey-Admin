// Sends a Web Push to every device the signed-in user has registered, except
// the one that made the change. The app calls this itself right after it
// writes, so there is no database webhook to configure.
//
// Secrets it needs (Edge Functions -> Secrets):
//   VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT
import { createClient } from "jsr:@supabase/supabase-js@2";
import * as webpush from "jsr:@negrel/webpush@0.3";

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

const WORD: Record<string, string> = {
  added: "New pick up",
  prepared: "Ready for collection",
  collected: "Collected",
};

let appServer: Awaited<ReturnType<typeof webpush.ApplicationServer.new>> | null = null;
async function server() {
  if (appServer) return appServer;
  const keys = await webpush.importVapidKeys(
    {
      publicKey: Deno.env.get("VAPID_PUBLIC_KEY")!,
      privateKey: Deno.env.get("VAPID_PRIVATE_KEY")!,
    },
    { extractable: false },
  );
  appServer = await webpush.ApplicationServer.new({
    contactInformation: Deno.env.get("VAPID_SUBJECT") ?? "mailto:admin@example.com",
    vapidKeys: keys,
  });
  return appServer;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });

  try {
    const authHeader = req.headers.get("Authorization") ?? "";
    if (!authHeader) return json({ error: "not signed in" }, 401);

    // the caller's own token, so row level security decides what they can read
    const sb = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_ANON_KEY")!,
      { global: { headers: { Authorization: authHeader } } },
    );
    const { data: { user } } = await sb.auth.getUser();
    if (!user) return json({ error: "not signed in" }, 401);

    const { kind, order_id, name, from } = await req.json();
    if (!WORD[kind]) return json({ error: "unknown kind" }, 400);

    const { data: subs, error } = await sb
      .from("push_subscriptions")
      .select("endpoint,p256dh,auth")
      .eq("user_id", user.id);
    if (error) return json({ error: error.message }, 500);

    // the phone that made the change already chimed; do not buzz it again
    const targets = (subs ?? []).filter((s) => s.endpoint !== from);
    if (!targets.length) return json({ sent: 0, note: "no other devices registered" });

    const payload = JSON.stringify({
      title: WORD[kind],
      body: name ? `${name} · ${order_id}` : String(order_id ?? ""),
      tag: `pickup-${order_id}`,
      url: "./",
    });

    const app = await server();
    let sent = 0;
    const gone: string[] = [];

    await Promise.all(targets.map(async (s) => {
      try {
        const subscriber = app.subscribe({
          endpoint: s.endpoint,
          keys: { p256dh: s.p256dh, auth: s.auth },
        });
        await subscriber.pushTextMessage(payload, { ttl: 600, urgency: "high" });
        sent++;
      } catch (e) {
        // 404/410 means the browser threw the subscription away
        const msg = String(e);
        if (msg.includes("404") || msg.includes("410") || msg.includes("Gone")) gone.push(s.endpoint);
        else console.error("push failed", s.endpoint.slice(0, 40), msg);
      }
    }));

    if (gone.length) await sb.from("push_subscriptions").delete().in("endpoint", gone);

    return json({ sent, dropped: gone.length });
  } catch (e) {
    console.error(e);
    return json({ error: String(e) }, 500);
  }
});
