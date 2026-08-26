// Copy the SKU photos that the browser could not.
//
// An imported sheet gives each SKU the supplier's own image address. The app
// tries to fetch each one and put a copy in our bucket, and for a good many
// hosts that fetch is refused — not because the image is missing, but because
// a browser on our domain is not allowed to read it. Nothing here is a
// browser, so there is no such rule: the fetch is plain server to server.
//
// Called by the app with a signed-in user's token:
//   { store: "partymonkey" }            one batch, default size
//   { store, limit: 25, offset: 0 }     the app's loop uses these
//
// Answers with { scanned, copied, failed, remaining }. The rows that fail stay
// at the front of the list, so the app adds the failures to `offset` and asks
// again until `scanned` comes back nought.
//
// It needs no secrets of its own — SUPABASE_URL, SUPABASE_ANON_KEY and
// SUPABASE_SERVICE_ROLE_KEY are already in every function's environment.
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

const BUCKET = "shipment-photos";
const MAX_BYTES = 8 * 1024 * 1024;   // a catalogue thumbnail, not a poster
const FETCH_MS = 20000;

// Some hosts serve an image to anything that looks like a person browsing and
// refuse everything else, so the request says it is a browser. Nothing is
// disguised beyond that: the address came from the supplier's own sheet.
const BROWSERISH = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
  "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
};

const EXT: Record<string, string> = {
  "image/jpeg": "jpg",
  "image/jpg": "jpg",
  "image/png": "png",
  "image/webp": "webp",
  "image/gif": "gif",
  "image/avif": "avif",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  try {
    const body = await req.json().catch(() => ({}));
    const store = String(body.store ?? "").trim();
    if (!store) return json({ error: "which shop?" }, 400);
    const limit = Math.min(50, Math.max(1, Number(body.limit) || 20));
    const offset = Math.max(0, Number(body.offset) || 0);

    const url = Deno.env.get("SUPABASE_URL")!;
    const anon = Deno.env.get("SUPABASE_ANON_KEY")!;
    const service = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const auth = req.headers.get("Authorization") ?? "";

    // The caller's own token decides who they are, and the team table decides
    // whether they may touch the shared catalogue.
    const asUser = createClient(url, anon, { global: { headers: { Authorization: auth } } });
    const { data: me } = await asUser.auth.getUser();
    if (!me?.user) return json({ error: "sign in first" }, 401);

    const admin = createClient(url, service);
    const { data: prof } = await admin.from("profiles")
      .select("user_id").eq("user_id", me.user.id).maybeSingle();
    if (!prof) return json({ error: "not on the team" }, 403);

    // the ones with an address and no copy, oldest name first so the order is
    // the same on every call
    const wanted = admin.from("warehouse_skus")
      .select("id,sku,photo_url", { count: "exact" })
      .eq("store", store).is("photo_path", null).not("photo_url", "is", null);
    const { data: rows, count, error } = await wanted
      .order("sku").range(offset, offset + limit - 1);
    if (error) return json({ error: error.message }, 500);

    const list = rows ?? [];
    let copied = 0, failed = 0;
    const trouble: string[] = [];

    for (const r of list) {
      try {
        const res = await fetch(r.photo_url, {
          headers: BROWSERISH,
          redirect: "follow",
          signal: AbortSignal.timeout(FETCH_MS),
        });
        if (!res.ok) throw new Error("HTTP " + res.status);
        const type = (res.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
        if (!type.startsWith("image/")) throw new Error("not an image (" + (type || "no type") + ")");
        const bytes = new Uint8Array(await res.arrayBuffer());
        if (!bytes.length) throw new Error("empty");
        if (bytes.length > MAX_BYTES) throw new Error("too big");

        const ext = EXT[type] ?? "jpg";
        // the same shape the app uses, so the team's read rule still finds it
        const path = `${me.user.id}/sku/${Date.now()}-${crypto.randomUUID()}.${ext}`;
        const up = await admin.storage.from(BUCKET)
          .upload(path, bytes, { contentType: type, upsert: false });
        if (up.error) throw up.error;

        const { error: wrote } = await admin.from("warehouse_skus")
          .update({ photo_path: path }).eq("id", r.id);
        if (wrote) {                       // an orphan file helps nobody
          await admin.storage.from(BUCKET).remove([path]);
          throw wrote;
        }
        copied++;
      } catch (e) {
        failed++;
        if (trouble.length < 5) {
          trouble.push(`${r.sku}: ${(e as Error)?.message ?? "could not be read"}`);
        }
      }
    }

    // what is left after this batch, on the assumption the failures stay put
    const total = count ?? (offset + list.length);
    return json({
      scanned: list.length,
      copied,
      failed,
      remaining: Math.max(0, total - offset - list.length),
      trouble,
    });
  } catch (err) {
    return json({ error: (err as Error)?.message ?? "unknown error" }, 500);
  }
});
