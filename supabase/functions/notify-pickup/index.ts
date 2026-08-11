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

function b64uToBytes(s: string): Uint8Array {
  const p = s.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(p + "=".repeat((4 - p.length % 4) % 4));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
const bytesToB64u = (b: Uint8Array) =>
  btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

// deno-lint-ignore no-explicit-any
let appServer: any = null;
async function server() {
  if (appServer) return appServer;
  // trim, because a space or newline pasted along with the value is invisible
  // in the dashboard but breaks the key
  const pub = (Deno.env.get("VAPID_PUBLIC_KEY") ?? "").trim();
  const prv = (Deno.env.get("VAPID_PRIVATE_KEY") ?? "").trim();
  if (!pub || !prv) {
    // list the names it can see — never the values — so a misspelled or
    // unsaved secret is obvious rather than a guessing game
    let seen = "none";
    try {
      const names = Object.keys(Deno.env.toObject()).filter((n) => /vapid/i.test(n)).sort();
      if (names.length) seen = names.join(", ");
    } catch { seen = "could not list"; }
    throw new Error(
      `${!pub ? "VAPID_PUBLIC_KEY" : "VAPID_PRIVATE_KEY"} is not set. ` +
      `Secrets containing "vapid" that this function can see: ${seen}. ` +
      `If the name looks right, redeploy the function so it picks up the value.`,
    );
  }
  if (pub.length < 80) throw new Error(`VAPID_PUBLIC_KEY looks wrong (${pub.length} chars, expected ~87)`);
  if (prv.length > 60) throw new Error(`VAPID_PRIVATE_KEY looks like the public key (${prv.length} chars, expected ~43)`);

  // importVapidKeys wants JWKs, not the base64url strings the push spec uses
  // on the wire, so unpack the point into its x and y halves here
  let keys;
  try {
    const raw = b64uToBytes(pub);
    if (raw.length !== 65 || raw[0] !== 4) {
      throw new Error(`public key is ${raw.length} bytes, expected a 65-byte uncompressed point`);
    }
    const x = bytesToB64u(raw.slice(1, 33));
    const y = bytesToB64u(raw.slice(33, 65));
    keys = await webpush.importVapidKeys(
      {
        publicKey: { kty: "EC", crv: "P-256", x, y },
        privateKey: { kty: "EC", crv: "P-256", x, y, d: prv },
      },
      { extractable: false },
    );
  } catch (e) {
    throw new Error("could not read the VAPID keys: " + String(e));
  }
  try {
    appServer = await webpush.ApplicationServer.new({
      contactInformation: Deno.env.get("VAPID_SUBJECT") ?? "mailto:admin@example.com",
      vapidKeys: keys,
    });
  } catch (e) {
    throw new Error("could not start the push server: " + String(e));
  }
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

    const { kind, order_id, name, from, test } = await req.json();
    if (!WORD[kind]) return json({ error: "unknown kind" }, 400);

    const { data: subs, error } = await sb
      .from("push_subscriptions")
      .select("endpoint,p256dh,auth")
      .eq("user_id", user.id);
    if (error) return json({ error: error.message }, 500);

    // the phone that made the change already chimed; do not buzz it again —
    // unless this is someone checking their own setup works
    const targets = test ? (subs ?? []) : (subs ?? []).filter((s) => s.endpoint !== from);
    if (!targets.length) return json({ sent: 0, note: "no other devices registered" });

    const payload = JSON.stringify({
      title: WORD[kind],
      body: name ? `${name} · ${order_id}` : String(order_id ?? ""),
      tag: `pickup-${order_id}`,
      url: "./",
    });

    let app;
    try { app = await server(); }
    catch (e) { return json({ error: (e as Error).message }, 500); }

    let sent = 0;
    const gone: string[] = [];
    const failures: string[] = [];

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
        else { failures.push(msg.slice(0, 160)); console.error("push failed", s.endpoint.slice(0, 40), msg); }
      }
    }));

    if (gone.length) await sb.from("push_subscriptions").delete().in("endpoint", gone);

    // a send that reached nobody is a failure worth reporting, not a quiet 200
    if (!sent && failures.length) return json({ error: "push rejected: " + failures[0] }, 500);
    return json({ sent, dropped: gone.length });
  } catch (e) {
    console.error(e);
    return json({ error: String(e) }, 500);
  }
});
