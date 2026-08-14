// The day's energy question, written to the week's theme.
//
// Called by the app with a signed-in user's token:
//   { day: "YYYY-MM-DD" }          the day in Singapore time, worked out by
//                                  the app — nothing here knows the shop's
//                                  timezone, the same as the rest of Energy
//   { day, force: true }           admin only: throw away what is stored for
//                                  that day and write it again
//
// Answers with { set: { question, modes, theme } }, or { off: true } when the
// switch is off, or { error } — the app falls back to the hand-written sets
// on anything but a set.
//
// WHY IT IS STORED RATHER THAN ASKED PER PERSON: the mix on the card only
// means something if everybody answered the same question, and a model asked
// the same thing twice does not say the same thing twice. So the first device
// to ask on a given day causes the question to be written, and every device
// after it reads what was written.
//
// Secrets it needs (Edge Functions -> Secrets):
//   GEMINI_API_KEY   required
//   GEMINI_MODEL     optional, defaults to gemini-3.6-flash
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

// Read per call, not once at boot: changing the GEMINI_MODEL secret should
// take effect on the next question rather than whenever a worker happens to be
// recycled. Models are retired on a schedule — 2.5 Flash closed to new keys
// well before its shutdown — so this will need changing again, and changing it
// should not mean a redeploy.
const model = () => Deno.env.get("GEMINI_MODEL") || "gemini-3.6-flash";

// The four slots always run most capacity first, least last. That ordering is
// load-bearing — it is what lets a Monday and a Friday be compared when the
// wording differs — so it is spelled out here, constrained by the schema
// below, and checked again on the way out.
const INSTRUCTION = `You write one short check-in question for a small
warehouse and print shop team, asked once a day on their phones.

The question asks how much capacity someone has today. It must be answerable
by tapping one of exactly four choices.

The four choices MUST be ordered from MOST capacity to LEAST capacity.
Position carries the meaning: the first is someone with plenty in the tank,
the fourth is someone who is running on empty. Never reorder them.

Each choice needs:
  ico  - one emoji, no text
  name - one or two words, fits under a circle on a phone
  hint - at most six words, saying what picking it means

Rules:
- Keep it warm and plain. No corporate language, no jargon, no exclamation marks.
- Never ask about anything private: health, money, family, mood in a clinical
  sense. Capacity for the day's work only.
- The theme colours the wording, it is not the subject. A theme of
  "the Christmas rush" gives a question about capacity during a rush, not a
  question about Christmas.
- Vary the wording day to day. Do not open with "How" every time.`;

const SCHEMA = {
  type: "object",
  properties: {
    question: { type: "string" },
    modes: {
      type: "array",
      minItems: 4,
      maxItems: 4,
      items: {
        type: "object",
        properties: {
          ico: { type: "string" },
          name: { type: "string" },
          hint: { type: "string" },
        },
        required: ["ico", "name", "hint"],
      },
    },
  },
  required: ["question", "modes"],
};

type Mode = { ico: string; name: string; hint: string };

// A model will cheerfully return three slots, or five, or an empty name. None
// of that may reach the table: the card renders exactly four and the mix is
// read by position.
function clean(raw: unknown): { question: string; modes: Mode[] } | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const question = String(o.question ?? "").trim();
  if (!question || question.length > 160) return null;
  if (!Array.isArray(o.modes) || o.modes.length !== 4) return null;
  const modes: Mode[] = [];
  for (const m of o.modes) {
    if (!m || typeof m !== "object") return null;
    const mm = m as Record<string, unknown>;
    const ico = String(mm.ico ?? "").trim();
    const name = String(mm.name ?? "").trim();
    const hint = String(mm.hint ?? "").trim();
    if (!ico || !name) return null;
    // trimmed rather than rejected: an over-long hint is a wrapping problem,
    // not a reason to fall back to yesterday's question
    modes.push({ ico: [...ico][0] ?? "•", name: name.slice(0, 18), hint: hint.slice(0, 48) });
  }
  return { question, modes };
}

// Names only, never values. "Not set" on its own cannot tell a missing secret
// from a misspelled one, and those want opposite fixes — so say what the
// function can actually see. Anything Supabase injects itself is filtered out;
// what is left is what somebody typed, which is where the mistake will be.
function secretNames() {
  try {
    return Object.keys(Deno.env.toObject())
      .filter((k) => !/^(SUPABASE_|SB_|DENO_|_)/.test(k))
      .sort();
  } catch { return []; }
}

async function writeQuestion(theme: string, day: string) {
  const key = Deno.env.get("GEMINI_API_KEY");
  if (!key) {
    const seen = secretNames();
    throw new Error(
      "GEMINI_API_KEY is not set on this function. " +
        (seen.length
          ? `Secrets it can see: ${seen.join(", ")}. ` +
            "If yours is in that list, the name differs — check for a stray space or lowercase."
          : "It can see no secrets at all, so either none were saved on this project " +
            "or the function is running an older deploy — deploy it again."),
    );
  }
  const name = model();
  const url =
    `https://generativelanguage.googleapis.com/v1beta/models/${name}:generateContent`;
  // The day goes in the prompt purely so two consecutive days do not come back
  // identical when the theme has not changed.
  const asked = theme.trim()
    ? `This week's theme: ${theme.trim()}\nToday is ${day}. Write today's question.`
    : `Today is ${day}. Write today's question. There is no theme this week, so keep it general.`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-goog-api-key": key },
    body: JSON.stringify({
      systemInstruction: { parts: [{ text: INSTRUCTION }] },
      contents: [{ role: "user", parts: [{ text: asked }] }],
      generationConfig: {
        temperature: 1.0,
        responseMimeType: "application/json",
        responseSchema: SCHEMA,
      },
    }),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    // A retired or misspelled model is the one failure worth naming outright,
    // because the fix is a secret rather than a code change and the reply does
    // not say which models this key may actually use.
    const hint = res.status === 404
      ? ` — "${name}" is not available to this key. Set the GEMINI_MODEL secret to one that is;` +
        ` the list is at https://generativelanguage.googleapis.com/v1beta/models`
      : "";
    throw new Error(`Gemini said ${res.status}${hint}: ${detail.slice(0, 240)}`);
  }
  const body = await res.json();
  const text = body?.candidates?.[0]?.content?.parts?.[0]?.text;
  if (!text) throw new Error("Gemini returned nothing to read");
  let parsed: unknown;
  try { parsed = JSON.parse(text); } catch { throw new Error("Gemini returned something that is not JSON"); }
  const ok = clean(parsed);
  if (!ok) throw new Error("Gemini returned a question that is not four ordered choices");
  return ok;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  try {
    const { day, force } = await req.json().catch(() => ({}));
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(day ?? ""))) return json({ error: "bad day" }, 400);

    const url = Deno.env.get("SUPABASE_URL")!;
    const anon = Deno.env.get("SUPABASE_ANON_KEY")!;
    const service = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const auth = req.headers.get("Authorization") ?? "";

    // The caller's own token decides whether they are on the team — the
    // function never takes their word for who they are.
    const asUser = createClient(url, anon, { global: { headers: { Authorization: auth } } });
    const { data: me } = await asUser.auth.getUser();
    if (!me?.user) return json({ error: "sign in first" }, 401);

    const admin = createClient(url, service);

    if (force) {
      const { data: prof } = await admin.from("profiles")
        .select("is_admin").eq("user_id", me.user.id).maybeSingle();
      if (!prof?.is_admin) return json({ error: "admins only" }, 403);
      await admin.from("energy_generated").delete().eq("day", day);
    } else {
      // already written today — this is the path nearly every call takes
      const { data: had } = await admin.from("energy_generated")
        .select("question,modes,theme").eq("day", day).maybeSingle();
      if (had) return json({ set: had });
    }

    const { data: t } = await admin.from("energy_theme")
      .select("theme,enabled").eq("id", true).maybeSingle();
    if (!t?.enabled) return json({ off: true });

    const written = await writeQuestion(t.theme ?? "", day);

    // Two phones opening the app at the same moment both find nothing and both
    // write. Ignoring the conflict and reading back means they agree on
    // whichever landed first, which is all that matters.
    await admin.from("energy_generated")
      .insert({ day, theme: t.theme ?? "", ...written })
      .select().maybeSingle();
    const { data: settled } = await admin.from("energy_generated")
      .select("question,modes,theme").eq("day", day).maybeSingle();

    return json({ set: settled ?? { ...written, theme: t.theme ?? "" } });
  } catch (err) {
    return json({ error: (err as Error)?.message ?? "unknown error" }, 500);
  }
});
