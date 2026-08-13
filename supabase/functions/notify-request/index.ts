// Web Push for Request & Reminder. Unlike notify-pickup, which tells a whole
// section, this one is addressed: a request goes to the person it was given
// to, and an acknowledgement goes back to the person who asked.
//
// It answers two kinds of call.
//
//   1. From the app, with a signed-in user's token:
//        { request_id, kind: "new" | "reminder" | "acknowledged" | "completed",
//          from?: <this device's endpoint> }
//      The caller's own token reads the request, so row level security decides
//      whether they are party to it — the function never takes their word for
//      who they are.
//
//   2. From pg_cron, with the public anon key in Authorization (to satisfy the
//      platform gateway) and the real password in an x-cron-secret header:
//        { due: true }
//      Sweeps every request whose snooze has run out and pushes the reminder,
//      which is what makes a snooze survive the app being closed. See PART 5
//      of supabase-migration-REQUESTS.sql.
//
// Secrets it needs (Edge Functions -> Secrets):
//   VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT
//   CRON_SECRET   — only for the scheduled sweep above. Make one up; it is a
//                   password shared between the cron job and this function.
import { createClient } from "jsr:@supabase/supabase-js@2";
import * as webpush from "jsr:@negrel/webpush@0.3";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  // x-cron-secret is listed so the sweep can be tried by hand from a browser.
  // pg_net never preflights, so the schedule itself does not depend on this —
  // but without it a browser refuses the call before it is sent, which is a
  // confusing way to lose an afternoon.
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-cron-secret",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });

type Kind = "new" | "reminder" | "acknowledged" | "completed" | "cancelled";
// who each kind is addressed to, and what it says. The two aimed at the
// assignee say so louder when the request is marked urgent — that word is the
// whole reason for the setting, and the notification is the only place most
// people will ever read it.
const KIND: Record<Kind, { to: "assignee" | "requester"; word: string; urgent?: string }> = {
  new:          { to: "assignee",  word: "New request", urgent: "URGENT request" },
  reminder:     { to: "assignee",  word: "Reminder",    urgent: "URGENT reminder" },
  acknowledged: { to: "requester", word: "Acknowledged" },
  completed:    { to: "requester", word: "Completed" },
  // Sent while the request still exists — once the row is gone there is
  // nothing left to read, and the person who was asked deserves to be told
  // they can stop.
  cancelled:    { to: "assignee",  word: "Request cancelled" },
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

const adminClient = () =>
  createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

// deno-lint-ignore no-explicit-any
type Msg = { userId: string; payload: Record<string, any>; skip?: string };

// Pushes each message to every device its account has registered. Reading
// other people's subscription rows is exactly what the service role is for —
// a requester's token cannot see the assignee's devices, and should not.
async function deliver(msgs: Msg[]) {
  if (!msgs.length) return { sent: 0, dropped: 0, failures: [] as string[] };
  const admin = adminClient();
  const { data: subs, error } = await admin
    .from("push_subscriptions")
    .select("user_id,endpoint,p256dh,auth")
    .in("user_id", [...new Set(msgs.map((m) => m.userId))]);
  if (error) throw new Error(error.message);

  const app = await server();
  let sent = 0;
  const gone: string[] = [];
  const failures: string[] = [];

  await Promise.all(msgs.flatMap((m) =>
    (subs ?? [])
      .filter((s) => s.user_id === m.userId)
      // the device that made the change has already chimed on its own
      .filter((s) => s.endpoint !== m.skip)
      .map(async (s) => {
        try {
          const subscriber = app.subscribe({
            endpoint: s.endpoint,
            keys: { p256dh: s.p256dh, auth: s.auth },
          });
          await subscriber.pushTextMessage(JSON.stringify(m.payload), { ttl: 600, urgency: "high" });
          sent++;
        } catch (e) {
          // 404/410 means the browser threw the subscription away
          const msg = String(e);
          if (msg.includes("404") || msg.includes("410") || msg.includes("Gone")) gone.push(s.endpoint);
          else { failures.push(msg.slice(0, 160)); console.error("push failed", s.endpoint.slice(0, 40), msg); }
        }
      })
  ));

  if (gone.length) await adminClient().from("push_subscriptions").delete().in("endpoint", gone);
  return { sent, dropped: gone.length, failures };
}

// deno-lint-ignore no-explicit-any
const payloadFor = (kind: Kind, r: any) => ({
  title: (r.urgent && KIND[kind].urgent) || KIND[kind].word,
  body: String(r.title ?? "").slice(0, 120),
  urgent: !!r.urgent,
  // one notification per request, so a reminder replaces the ring before it
  // rather than stacking up a column of the same thing
  tag: `request-${r.id}`,
  url: "./",
  request_id: r.id,
  kind,
});

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });

  try {
    const authHeader = req.headers.get("Authorization") ?? "";
    const body = await req.json().catch(() => ({}));

    // The sweep is checked before the signed-in requirement further down,
    // because it has no user and carries no Authorization header at all — its
    // password travels in x-cron-secret. Demanding one here turned every
    // scheduled run into a 401 that never reached the check that mattered.
    // ---- the cron sweep ----------------------------------------------
    // Only the service role may ask for this: it reads and writes rows on
    // everybody's behalf, which no signed-in user should be able to trigger.
    if (body.due) {
      // The password arrives in its own header, never in Authorization.
      // With "Verify JWT" on, the platform rejects anything in Authorization
      // that is not a real token before this code runs — and pg_net could not
      // be made to deliver one it would accept, whatever was put there. So
      // the function is deployed with that check off and the scheduled job
      // sends no Authorization at all; this header is the whole of it.
      const presented = (req.headers.get("x-cron-secret") ?? "").trim() ||
                        authHeader.replace(/^Bearer\s+/i, "").trim();
      // CRON_SECRET is a password you make up and set once in the function's
      // secrets. Prefer it: the scheduled job lives in a SQL statement that
      // sits in the database for anyone with access to read, and this is a
      // password that only triggers reminder pushes. The service role key
      // pasted in the same place would read and rewrite the whole project.
      const cronSecret = (Deno.env.get("CRON_SECRET") ?? "").trim();
      const serviceKey = (Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "").trim();
      const ok = (cronSecret && presented === cronSecret) ||
                 (serviceKey && presented === serviceKey);
      if (!ok) {
        return json({
          error: "the due sweep needs the x-cron-secret header" +
                 (cronSecret ? " to match CRON_SECRET" : " — but CRON_SECRET is not set on this function"),
        }, 403);
      }
      const admin = adminClient();
      const { data: due, error } = await admin
        .from("requests")
        .select("id,title,assignee_id,urgent")
        .neq("status", "completed")
        .not("remind_at", "is", null)
        .lte("remind_at", new Date().toISOString())
        .limit(200);
      if (error) return json({ error: error.message }, 500);
      if (!due?.length) return json({ due: 0, sent: 0 });

      // Cleared before the push, not after. A send that fails is one missed
      // ring; a send that succeeds without the row being cleared would ring
      // again every minute until somebody acted, which is far worse.
      const { error: clearErr } = await admin
        .from("requests").update({ remind_at: null }).in("id", due.map((r) => r.id));
      if (clearErr) return json({ error: clearErr.message }, 500);

      const out = await deliver(due.map((r) => ({
        userId: r.assignee_id,
        payload: payloadFor("reminder", r),
      })));
      return json({ due: due.length, ...out, failures: out.failures.slice(0, 1) });
    }

    // ---- a push the app asked for ------------------------------------
    // everything past here is on behalf of a signed-in person, so from here on
    // a token is required
    if (!authHeader) return json({ error: "not signed in" }, 401);
    const kind = body.kind as Kind;
    if (!KIND[kind]) return json({ error: "unknown kind" }, 400);
    if (!body.request_id) return json({ error: "no request_id" }, 400);

    // the caller's own token, so row level security decides what they can read
    const sb = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_ANON_KEY")!,
      { global: { headers: { Authorization: authHeader } } },
    );
    const { data: { user } } = await sb.auth.getUser();
    if (!user) return json({ error: "not signed in" }, 401);

    // If they are not party to this request the select returns nothing, which
    // is the whole authorisation check — no id can be guessed into a push.
    const { data: r, error } = await sb
      .from("requests")
      .select("id,title,requester_id,assignee_id,urgent")
      .eq("id", body.request_id)
      .maybeSingle();
    if (error) return json({ error: error.message }, 500);
    if (!r) return json({ error: "no such request" }, 404);

    const target = KIND[kind].to === "assignee" ? r.assignee_id : r.requester_id;
    // and the direction has to match who is asking: an assignee cannot send
    // themselves a "new request", nor a requester an acknowledgement
    const senderShouldBe = KIND[kind].to === "assignee" ? r.requester_id : r.assignee_id;
    if (user.id !== senderShouldBe) return json({ error: "not yours to send" }, 403);

    let out;
    try {
      out = await deliver([{ userId: target, payload: payloadFor(kind, r), skip: body.from }]);
    } catch (e) {
      return json({ error: (e as Error).message }, 500);
    }
    if (!out.sent && out.failures.length) return json({ error: "push rejected: " + out.failures[0] }, 500);
    if (!out.sent) return json({ sent: 0, note: "that account has no device registered" });
    return json({ sent: out.sent, dropped: out.dropped });
  } catch (e) {
    console.error(e);
    return json({ error: String(e) }, 500);
  }
});
