// The draft answer to a buyer's chat message, written in the shop's own voice.
//
// Called by the app with a signed-in user's token:
//   { message }                   draft a reply to this
//   { message, lang }             ... in this language rather than the buyer's
//   { message, tone }             ... rewritten: shorter, warmer, declining
//   { message, history }          ... with the lines said before it, oldest
//                                 first, as [{inbound, text}]
//   { distil: true }              read the approved replies back and say what
//                                 standing facts they contain
//
// Answers with { draft, used, photos }: used is the ids of the approved replies
// the draft was written from — the screen shows them, so it is never a mystery
// why a draft said what it said — and photos are the pictures attached to the
// facts this question matched, offered for the screen to tick. Or { facts } for
// a distil. Or { error }.
//
// WHY THE PROMPT IS BUILT HERE AND NOT IN THE APP: the two things that keep a
// draft honest are the shop's facts and the replies already approved, and both
// are rows this function can read for itself. An app that assembled the prompt
// would be an app that could be out of date with the table, and a phone on a
// slow connection would be uploading the whole voice of the shop on every
// message. The app sends the buyer's message and nothing else.
//
// NOTHING IS SENT ANYWHERE. This writes a draft and returns it. The reply
// leaves for the buyer only when somebody reads it and presses Enter, and even
// then it is Macro Studio on the shop PC that types it into DuoKe.
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

// Read per call rather than once at boot, for the same reason as the energy
// question: changing the GEMINI_MODEL secret should take effect on the next
// draft, not whenever a worker happens to be recycled.
// "gemini-3.6-flash" was the default and this key cannot call it: the name
// was wrong, so every draft fell through to the fallbacks. An alias is the
// right kind of default -- it survives the retirements that break a pinned
// version, which is the failure this whole ladder exists to absorb.
const model = () => Deno.env.get("GEMINI_MODEL") || "gemini-flash-latest";

// Names only, never values — "not set" cannot tell a missing secret from a
// misspelled one, and those want opposite fixes.
function secretNames() {
  try {
    return Object.keys(Deno.env.toObject())
      .filter((k) => !/^(SUPABASE_|SB_|DENO_|_)/.test(k))
      .sort();
  } catch { return []; }
}

function geminiKey() {
  const key = Deno.env.get("GEMINI_API_KEY");
  if (key) return key;
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

// The answer, and only the answer.
//
// A thinking model replies in several parts, and the ones it thought with are
// marked `thought: true`. Reading parts[0] blind put "Analyze Buyer Query &
// Match to Shop Facts:**" in the draft box — the model's own working, shown to
// a buyer. So the thought parts are dropped and what is left is joined.
function answerText(out: unknown): string {
  const parts = (out as {
    candidates?: { content?: { parts?: { text?: string; thought?: boolean }[] } }[];
  })?.candidates?.[0]?.content?.parts ?? [];
  return parts
    .filter((p) => p?.thought !== true && typeof p?.text === "string")
    .map((p) => p.text as string)
    .join("")
    .trim();
}

// What this key may actually call. Asked only when a request has already been
// refused: the answer is what turns "invalid argument" into something a person
// can act on, because the refusal itself names no field and no model.
async function usableModels(): Promise<string[]> {
  try {
    const res = await fetch("https://generativelanguage.googleapis.com/v1beta/models", {
      headers: { "x-goog-api-key": geminiKey() },
    });
    if (!res.ok) return [];
    const body = await res.json();
    const models = (body as {
      models?: { name?: string; supportedGenerationMethods?: string[] }[];
    })?.models ?? [];
    return models
      .filter((m) => (m?.supportedGenerationMethods ?? []).includes("generateContent"))
      .map((m) => String(m?.name ?? "").replace(/^models\//, ""))
      .filter((n) => n && !/embedding|aqa|imagen|veo|tts/i.test(n));
  } catch { return []; }
}

async function ask(body: Record<string, unknown>) {
  // The model from the secret is tried first, then two that have outlived
  // several retirements. A model name that is wrong is answered 404 by some
  // endpoints and 400 by others, so a refusal is never taken as proof the
  // request was the problem — the next name is tried before giving up.
  // Lite last on purpose: it is the one with room left when the free tier's
  // per-minute allowance on the bigger models has gone, which is what a 429
  // here actually means.
  const names = [...new Set([model(), "gemini-flash-latest", "gemini-2.5-flash",
                             "gemini-2.5-flash-lite"])];
  const send = (name: string, payload: unknown) =>
    fetch(`https://generativelanguage.googleapis.com/v1beta/models/${name}:generateContent`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": geminiKey() },
      body: JSON.stringify(payload),
    });

  // Thinking off where the model allows it. A two-sentence chat reply has no
  // use for it, and left on it spends the whole output budget before the reply
  // is written — which is how a draft once came back as a bare asterisk.
  //
  // Which models take which switch is a moving target: some want
  // thinkingConfig.thinkingBudget, some want thinkingLevel, and the ones that
  // always think reject both. Worse, the refusal is a bare "Request contains an
  // invalid argument" that names no field — so the asking is a ladder rather
  // than a guess, and the rung that works is the one used. A model that
  // refuses every rung is asked plainly, with room to think AND answer, since
  // a thinking model's reply only needs the thoughts dropped, which
  // answerText does anyway.
  const { generationConfig, ...rest } = body as { generationConfig?: Record<string, unknown> };
  const config = { ...(generationConfig ?? {}) };
  const asked = Number(config.maxOutputTokens ?? 700);
  const rungs: Record<string, unknown>[] = [
    { ...rest, generationConfig: { ...config, thinkingConfig: { thinkingBudget: 0 } } },
    { ...rest, generationConfig: { ...config, thinkingConfig: { thinkingLevel: "low" } } },
    // No switch, and three times the room: the thinking is paid for in tokens
    // here rather than in a truncated reply.
    { ...rest, generationConfig: { ...config, maxOutputTokens: Math.max(asked, 2400) } },
  ];
  // Every rung of every name, stopping the moment one is answered. The first
  // refusal is the one kept: a later one says even less than the first.
  let res: Response | null = null;
  let refused = "";
  let refusedBy = "";
  let busy = "";
  let settled = false;
  for (const name of names) {
    for (const rung of rungs) {
      const attempt = await send(name, rung);
      // Answered: done. Out of quota or overloaded: this MODEL is spent, but
      // another one has its own allowance, so break out of the rungs and try
      // the next name rather than giving up — the free tier runs out per
      // model, not per key. Anything else (a dead key, a refusal) no other
      // name will fix either.
      if (attempt.ok) {
        res = attempt;
        settled = true;
        break;
      }
      if (attempt.status === 429 || attempt.status === 503) {
        busy = await attempt.text().catch(() => "") || busy;
        res = attempt;
        break;
      }
      if (attempt.status !== 400 && attempt.status !== 404) {
        res = attempt;
        settled = true;
        break;
      }
      const detail = await attempt.text().catch(() => "");
      if (!refused) { refused = detail; refusedBy = name; }
      res = attempt;
    }
    if (settled) break;
  }
  if (!res || !res.ok) {
    const status = res?.status ?? 0;

    // Out of quota is not a misconfiguration, and listing the models a key may
    // call is unhelpful noise when the key may call all of them and has simply
    // run out for now. Say what it is and what to do about it.
    if (status === 429 || status === 503) {
      throw new Error(
        "Gemini is out of allowance for this key right now — every model it can reach " +
        `answered ${status}. The free tier resets by the minute and by the day, so this ` +
        "usually clears within a minute; if it keeps happening, set the GEMINI_MODEL " +
        "secret to gemini-2.5-flash-lite, which has the largest free allowance." +
        ` (${busy.slice(0, 120)})`,
      );
    }

    const detail = res && !refused ? await res.text().catch(() => "") : refused;
    // Which models the key may call is the answer nine times in ten, and the
    // refusal never says. So it is asked and put in the message.
    const can = await usableModels();
    const hint = can.length
      ? ` — "${refusedBy || names[0]}" was refused. This key can call: ${can.slice(0, 10).join(", ")}` +
        `${can.length > 10 ? ", …" : ""}. Set the GEMINI_MODEL secret to one of those.`
      : " — and this key could not list any models at all, so check GEMINI_API_KEY itself" +
        " (a restricted key, or one from a project without the Generative Language API enabled).";
    throw new Error(`Gemini said ${status}${hint}: ${detail.slice(0, 200)}`);
  }
  const out = await res.json();
  const text = answerText(out);
  if (!text) {
    // A model that thought until it ran out of room returns a candidate with
    // no answer in it at all. Say which of the two happened.
    const why = (out as { candidates?: { finishReason?: string }[] })?.candidates?.[0]?.finishReason;
    throw new Error(
      why === "MAX_TOKENS"
        ? "Gemini ran out of room before it wrote the reply — try Rewrite, or shorten the message"
        : "Gemini returned nothing to read",
    );
  }
  return text;
}

// ---- which approved replies look like this question ----
//
// Word overlap, not meaning. It is crude on purpose: a shop's chat is the same
// two dozen questions in the same two dozen words, and "post" matching "post"
// finds the right example far more reliably than anything clever would on
// mixed Malay-English typed at speed. The hits bonus is what lets a reply that
// keeps proving useful beat a closer-worded one that never does.
//
// The stop list is deliberately bilingual: without the Malay function words a
// question like "kak bila nak post ya" scores every example in the table.
const STOP = new Set((
  "a an the and or but is are was were be been am i you he she it we they me my your our " +
  "this that these those to of in on at for with from by as so if then than do does did " +
  "not no yes can could will would shall should may might must have has had ok okay pls " +
  "please thanks thank kak bang bro sis boleh nak tak ya la lah ke kah saya awak aku dia " +
  "kami kita ini itu dan atau dengan untuk dari pada yang ada dah sudah belum bila macam " +
  "mana kalau sama juga lagi bagi dapat mau mahu gak sih dong kok ada nya"
).split(" "));

function words(s: string) {
  return String(s ?? "").toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, " ")
    .split(/\s+/)
    .filter((w) => w.length > 2 && !STOP.has(w));
}

type Example = { id: string; buyer_text: string; reply_text: string; hits: number; edited: boolean };
type Fact = { fact: string; photos: string[] };

// Which attached photo, if any, this question is asking to be shown.
//
// The same word overlap as the examples, and for the same reason: a buyer
// asking about sizing uses the words that are written on the size chart. Two
// at most, because a reply that arrives with four pictures is worse than one
// that arrives with the right one — and none at all on a thin overlap, since
// an unasked-for photo is something a buyer has to scroll past.
function photosFor(message: string, facts: Fact[]): Fact[] {
  const asked = new Set(words(message));
  if (!asked.size) return [];
  const scored = facts
    .filter((f) => f.photos.length)
    .map((f) => {
      let score = 0;
      for (const w of words(f.fact)) if (asked.has(w)) score += 1;
      return { f, score };
    })
    .filter((x) => x.score >= 1);
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, 2).map((x) => x.f);
}

function pick(message: string, rows: Example[], want = 6): Example[] {
  const asked = [...new Set(words(message))];
  if (!asked.length) {
    // Nothing to match on — a sticker, an order number on its own. The most
    // used replies are still a better guide to the shop's voice than nothing.
    return rows.slice().sort((a, b) => (b.hits ?? 0) - (a.hits ?? 0)).slice(0, 3);
  }
  const scored = rows.map((row) => {
    const has = new Set(words(row.buyer_text + " " + row.reply_text));
    let score = 0;
    for (const w of asked) {
      if (has.has(w)) score += 1;
      else if ([...has].some((h) => h.startsWith(w) || w.startsWith(h))) score += 0.5;
    }
    // an edited reply is a correction somebody bothered to make; it is worth
    // more as an example than a draft that happened to be accepted as written
    if (row.edited) score += 0.25;
    return { row, score: score + Math.min(1.2, (row.hits ?? 0) * 0.15) };
  }).filter((s) => s.score > 0.5);
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, want).map((s) => s.row);
}

// ---- the prompt ----
type Line = { inbound?: boolean; text?: string };

function draftPrompt(
  message: string,
  facts: Fact[],
  examples: Example[],
  lang: string,
  tone: string,
  store: string,
  history: Line[],
) {
  const lines: string[] = [];
  lines.push(
    "You write chat replies for a small Shopee seller answering a buyer in Shopee Chat. " +
    "Write the reply the seller will send" + (store ? ` from the shop "${store}"` : "") + ".",
  );
  lines.push("");
  lines.push("SHOP FACTS — the only facts you may state:");
  lines.push(
    facts.length
      ? facts.map((f) =>
        "- " + f.fact + (f.photos.length
          ? (f.photos.length > 1
            ? `   [${f.photos.length} photos of this go out with your reply]`
            : "   [a photo of this goes out with your reply]")
          : ""))
        .join("\n")
      : "- (none recorded yet)",
  );
  lines.push("");
  if (examples.length) {
    lines.push("REPLIES THIS SELLER HAS ALREADY APPROVED — copy this voice, wording and length:");
    for (const ex of examples) {
      lines.push(`Buyer: ${ex.buyer_text}\nSeller: ${ex.reply_text}\n`);
    }
  } else {
    lines.push(
      "No approved replies yet, so there is no voice to copy — write plainly and warmly, " +
      "the way a small seller types in chat.",
    );
    lines.push("");
  }
  lines.push("RULES");
  lines.push(
    lang
      ? `- Write in ${lang}.`
      : "- Write in the same language and register the buyer used, mixed Malay-English included. " +
        "Keep Shopee's own words as they are: tracking no, COD, variation, Return/Refund, Shopee Xpress.",
  );
  lines.push("- One to three short sentences. This is chat, not email: no greeting block, no sign-off.");
  lines.push("- No emoji unless the approved replies above use them.");
  lines.push(
    "- Never invent a posting date, tracking number, price, discount, stock level or courier " +
    "that is not in SHOP FACTS. Where the fact is missing, say you will check, or ask for the " +
    "order number.",
  );
  lines.push("- Never promise a refund amount, and never accept blame for a courier's delay.");
  if (facts.some((f) => f.photos.length)) {
    lines.push(
      "- Where a fact above is marked as having a photo going out and that fact answers the " +
      "question, say the picture is there — \"ni size chart dia\", \"here is the photo\" — in the " +
      "buyer's own language. It is attached to the same reply, so do not describe what is in it " +
      "and do not offer to send it later.",
    );
  }
  lines.push("- Do not apologise twice, and do not thank them twice.");
  if (tone) lines.push(`- This is a rewrite of a draft that was not right: make it ${tone}.`);
  lines.push(
    "- Output the reply text and nothing else. No quotes around it, no explanation, " +
    "no headings, no bullet points, no markdown, no working out. The first character " +
    "you write is the first character the buyer reads.",
  );
  lines.push("");
  if (history.length) {
    // Before the message, not after it: a model reads an instruction better
    // when the thing it is about has already been described.
    lines.push("THE CONVERSATION SO FAR — oldest first, for context only:");
    for (const line of history) {
      const text = String(line?.text ?? "").trim();
      if (text) lines.push((line?.inbound ? "Buyer: " : "You: ") + text);
    }
    lines.push("");
    lines.push(
      "Answer only the last message below. The lines above are what makes it make sense — " +
      "a buyer asking \"so tomorrow?\" is asking about whatever was being discussed. " +
      "Do not repeat what you have already told them, and do not greet them again.",
    );
    lines.push("");
  }
  lines.push("BUYER MESSAGE:");
  lines.push(message);
  return lines.join("\n");
}

const DISTIL_SCHEMA = {
  type: "object",
  properties: {
    facts: {
      type: "array",
      maxItems: 10,
      items: { type: "string" },
    },
  },
  required: ["facts"],
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  try {
    const body = await req.json().catch(() => ({}));
    const message = String(body?.message ?? "").trim();
    const distil = body?.distil === true;
    if (!distil && !message) return json({ error: "nothing to reply to" }, 400);
    if (message.length > 4000) return json({ error: "that message is too long to draft from" }, 400);

    const url = Deno.env.get("SUPABASE_URL")!;
    const anon = Deno.env.get("SUPABASE_ANON_KEY")!;
    const auth = req.headers.get("Authorization") ?? "";

    // The caller's own token reads the tables, so row level security decides
    // what this can see exactly as it does in the app. There is no service
    // role key in here at all: a draft needs nothing the person asking for it
    // could not read themselves.
    const asUser = createClient(url, anon, { global: { headers: { Authorization: auth } } });
    const { data: me } = await asUser.auth.getUser();
    if (!me?.user) return json({ error: "sign in first" }, 401);

    // select * rather than naming the columns, so a fact still loads on a
    // project where the photo migration has not been run yet
    const { data: factRows } = await asUser.from("reply_facts")
      .select("*").order("created_at");
    const facts: Fact[] = (factRows ?? [])
      .map((r) => {
        const row = r as { fact?: unknown; photo_path?: unknown; photos?: unknown };
        // A fact may carry several pictures now. photo_path is the first of
        // them, and on a project without that migration it is the only one.
        const many = Array.isArray(row.photos) ? row.photos.map((x) => String(x ?? "").trim()) : [];
        const one = String(row.photo_path ?? "").trim();
        return {
          fact: String(row.fact ?? "").trim(),
          photos: [...new Set([one, ...many].filter(Boolean))],
        };
      })
      .filter((f) => f.fact);

    // The whole table, ranked here. It is a few hundred rows of chat at the
    // very most — a shop that has approved a thousand replies has a thousand
    // short lines, not a corpus — and ranking needs to see all of them.
    const { data: exRows } = await asUser.from("reply_examples")
      .select("id,buyer_text,reply_text,hits,edited,created_at")
      .order("created_at", { ascending: false })
      .limit(1000);
    const examples = (exRows ?? []) as Example[];

    if (distil) {
      if (examples.length < 3) {
        return json({ facts: [], note: "approve a few replies first — there is nothing to read yet" });
      }
      const recent = examples.slice(0, 40);
      const text = await ask({
        systemInstruction: {
          parts: [{
            text:
              "You read replies a Shopee seller actually sent to buyers and pull out the standing " +
              "facts about the shop: posting times, couriers, stock and restock habits, price and " +
              "discount policy, exchange and refund rules — anything the seller repeats.\n\n" +
              "Each fact is one short line in the seller's own terms, under 110 characters.\n" +
              "Skip anything true of only one order. Skip anything the replies do not actually " +
              "support. Better to return three facts than ten guesses.",
          }],
        },
        contents: [{
          role: "user",
          parts: [{
            text: recent.map((r) => `Buyer: ${r.buyer_text}\nSeller: ${r.reply_text}`).join("\n\n"),
          }],
        }],
        generationConfig: {
          temperature: 0.2,
          responseMimeType: "application/json",
          responseSchema: DISTIL_SCHEMA,
          maxOutputTokens: 900,
        },
      });
      let parsed: unknown;
      try { parsed = JSON.parse(text); } catch { throw new Error("Gemini returned something that is not JSON"); }
      const got = (parsed as { facts?: unknown })?.facts;
      const out = Array.isArray(got)
        ? got.map((f) => String(f ?? "").trim().slice(0, 160)).filter(Boolean).slice(0, 10)
        : [];
      // Not written to the table here. A distilled fact is a suggestion about
      // the shop, and the person on the screen is the one who knows whether it
      // is true — they keep the ones that are.
      return json({ facts: out });
    }

    const used = pick(message, examples);
    // Worked out before the draft rather than after: a reply that says "here
    // is the chart" with no chart attached is worse than one that never
    // mentioned a chart at all.
    const offered = photosFor(message, facts);
    const draft = (await ask({
      contents: [{
        role: "user",
        parts: [{
          text: draftPrompt(
            message,
            [...offered, ...facts.filter((f) => !offered.includes(f))],
            used,
            String(body?.lang ?? "").trim().slice(0, 40),
            String(body?.tone ?? "").trim().slice(0, 120),
            String(body?.store ?? "").trim().slice(0, 60),
            // The last dozen lines at most, and each one short: the history is
            // context, and a whole morning of it would crowd out the facts and
            // the voice, which are what the draft is actually built from.
            (Array.isArray(body?.history) ? body.history : [])
              .slice(-12)
              .map((l: Line) => ({
                inbound: l?.inbound === true,
                text: String(l?.text ?? "").slice(0, 400),
              }))
              .filter((l) => l.text),
          ),
        }],
      }],
      generationConfig: {
        // Low, but not zero: a rewrite asked for because the first draft was
        // not right has to come back different.
        temperature: body?.tone ? 0.8 : 0.4,
        // Room for a long reply in a script with no short words, and no more:
        // a draft that arrives as an essay is the wrong answer anyway.
        maxOutputTokens: 700,
      },
    })).trim()
      // A model told "no quotes around it" still quotes it sometimes, and a
      // reply that arrives inside quotation marks gets pasted inside them too.
      .replace(/^["'“”]+|["'“”]+$/g, "")
      // Chat has no markdown. Asterisks and backticks that arrive around a
      // word are typed to the buyer literally, so they come off here.
      .replace(/\*\*([^*]+)\*\*/g, "$1")
      .replace(/(^|\s)[*_`]+|[*_`]+(?=\s|$)/g, "$1")
      .trim();

    if (!draft) return json({ error: "the draft came back empty — try again" }, 502);

    return json({
      draft,
      used: used.map((u) => u.id),
      // Offered, not decided: the screen shows these beside the draft with a
      // tick each, so nothing goes to a buyer that nobody looked at.
      // every picture the matched facts carry, capped so a reply cannot
      // arrive as an album
      photos: offered.flatMap((f) => f.photos.map((path) => ({ path, fact: f.fact })))
        .slice(0, 4),
    });
  } catch (err) {
    return json({ error: (err as Error)?.message ?? "unknown error" }, 500);
  }
});
