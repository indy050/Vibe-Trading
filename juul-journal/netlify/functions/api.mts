// Juul's journal API. Every route needs `Authorization: Bearer <access code>`.
// The code is the JOURNAL_TOKEN environment variable when set; otherwise its
// SHA-256 hash is claimed once through POST /api/setup and kept in Blobs.
// Storage: one Blobs key per trade/deposit, so no route does read-modify-write.
import { getStore } from "@netlify/blobs";
import type { Config, Context } from "@netlify/functions";
import { createHash, timingSafeEqual } from "node:crypto";

const store = () => getStore({ name: "journal", consistency: "strong" });
const ID = /^[A-Za-z0-9_.:-]{1,80}$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" } });
const fail = (status: number, error: string) => json({ error }, status);

const sha256 = (s: string) => createHash("sha256").update(s).digest();

async function authorized(req: Request): Promise<boolean> {
  const given = (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
  if (!given) return false;
  const env = Netlify.env.get("JOURNAL_TOKEN");
  const expected = env ? sha256(env) : await storedHash();
  return expected !== null && timingSafeEqual(sha256(given), expected);
}

async function storedHash(): Promise<Buffer | null> {
  const auth = (await store().get("auth", { type: "json" })) as { hash?: string } | null;
  return auth?.hash ? Buffer.from(auth.hash, "hex") : null;
}

async function setup(req: Request): Promise<Response> {
  if (Netlify.env.get("JOURNAL_TOKEN") || (await storedHash())) return fail(409, "De toegangscode is al ingesteld.");
  const { token } = await req.json().catch(() => ({ token: "" }));
  if (typeof token !== "string" || token.length < 16) return fail(400, "Kies een toegangscode van minstens 16 tekens.");
  const { modified } = await store().setJSON("auth", { hash: sha256(token).toString("hex"), created: new Date().toISOString() }, { onlyIfNew: true });
  return modified ? json({ ok: true }) : fail(409, "De toegangscode is al ingesteld.");
}

const num = (v: unknown): number | null => {
  if (v === null || v === undefined || v === "") return null;
  const x = Number(String(v).replace(/\s/g, "").replace(",", "."));
  return Number.isFinite(x) ? x : null;
};
const str = (v: unknown, max = 200) => (v === null || v === undefined ? "" : String(v).slice(0, max));

function cleanTrade(b: Record<string, unknown>, prev: Record<string, unknown> | null) {
  const date = str(b.date, 10);
  if (!DATE.test(date)) throw new Error("date moet JJJJ-MM-DD zijn");
  const result = num(b.result);
  if (result === null) throw new Error("result moet een getal zijn");
  const dir = b.dir === "Long" || b.dir === "Short" ? b.dir : null;
  const session = ["london", "ny", "asia"].includes(String(b.session)) ? String(b.session) : null;
  const now = new Date().toISOString();
  return {
    date, pair: str(b.pair, 20) || "US30", dir, session,
    entry: num(b.entry), sl: num(b.sl), tp: num(b.tp), lots: num(b.lots), exit: num(b.exit), result,
    r: num(b.r), rulesFollowed: typeof b.rulesFollowed === "boolean" ? b.rulesFollowed : null,
    notes: str(b.notes, 2000), source: str(b.source, 20) || "handmatig",
    tpId: num(b.tpId) ?? num(prev?.tpId) ?? Date.now(),
    created: str(prev?.created ?? b.created, 40) || now, updated: now,
  };
}

async function listJSON(prefix: string) {
  const s = store();
  const { blobs } = await s.list({ prefix });
  const items = await Promise.all(blobs.map(async b => ({ id: b.key.slice(prefix.length), ...(await s.get(b.key, { type: "json" })) })));
  return items.filter(x => x && x.id);
}

async function state() {
  const [trades, deposits, settings] = await Promise.all([listJSON("trades/"), listJSON("deposits/"), store().get("settings", { type: "json" })]);
  return { trades, deposits, settings: settings ?? { startCapital: 10000 } };
}

const safeId = (raw: unknown) => "tp-" + String(raw).replace(/[^A-Za-z0-9_.:-]/g, "").slice(0, 70);

async function importBackup(body: any) {
  if (!body || !Array.isArray(body.trades)) throw new Error("geen Trade Portal-back-up: trades ontbreekt");
  const s = store();
  let added = 0, skipped = 0, deps = 0;
  for (const t of body.trades) {
    if (!t || !DATE.test(String(t.date ?? ""))) { skipped++; continue; }
    const doc = cleanTrade({ ...t, tp: t.tp1 ?? t.tp, tpId: t.id,
      rulesFollowed: /regels gevolgd/i.test(t.notes ?? "") ? true : undefined,
      source: /\((engine|demo|backtest)\)/.test(t.notes ?? "") ? "juul" : "import" }, null);
    const { modified } = await s.setJSON("trades/" + safeId(t.id), doc, { onlyIfNew: true });
    modified ? added++ : skipped++;
  }
  for (const d of Array.isArray(body.deposits) ? body.deposits : []) {
    if (!d || !DATE.test(String(d.date ?? ""))) continue;
    const { modified } = await s.setJSON("deposits/" + safeId(d.id), { date: d.date, amount: num(d.amount) ?? 0, tpId: num(d.id) }, { onlyIfNew: true });
    if (modified) deps++;
  }
  return { added, skipped, deposits: deps };
}

function toBackup(st: Awaited<ReturnType<typeof state>>) {
  const fx = (v: unknown) => (num(v) === null ? "" : String(num(v)));
  return {
    version: 1, exported: new Date().toISOString(),
    trades: st.trades.map((t: any) => ({ id: num(t.tpId) ?? Date.now(), date: t.date, pair: t.pair, dir: t.dir ?? null,
      entry: fx(t.entry), sl: fx(t.sl), tp1: fx(t.tp), lots: fx(t.lots), exit: fx(t.exit),
      result: (num(t.result) ?? 0).toFixed(2), notes: t.notes ?? "", shot: null })),
    deposits: st.deposits.map((d: any) => ({ id: num(d.tpId) ?? Date.now(), date: d.date, amount: num(d.amount) ?? 0 })),
  };
}

export default async (req: Request, _context: Context) => {
  const parts = new URL(req.url).pathname.replace(/^\/api\/?/, "").split("/").filter(Boolean);
  if (req.method === "POST" && parts[0] === "setup") return setup(req);
  if (!(await authorized(req))) return fail(401, "Ongeldige of ontbrekende toegangscode.");
  const [res, id] = parts;
  try {
    if (req.method === "GET" && res === "state") return json(await state());
    if (req.method === "GET" && res === "export") return json(toBackup(await state()));
    if (req.method === "POST" && res === "import") return json(await importBackup(await req.json()));
    if (res === "settings" && req.method === "PUT") {
      const b = await req.json();
      const startCapital = num(b.startCapital);
      if (startCapital === null) return fail(400, "startCapital moet een getal zijn");
      await store().setJSON("settings", { startCapital });
      return json({ ok: true });
    }
    if ((res === "trades" || res === "deposits") && id) {
      if (!ID.test(id)) return fail(400, "ongeldige id");
      const key = `${res}/${id}`;
      if (req.method === "DELETE") { await store().delete(key); return json({ ok: true }); }
      if (req.method === "PUT") {
        const b = await req.json();
        if (res === "deposits") {
          if (!DATE.test(String(b.date ?? ""))) return fail(400, "date moet JJJJ-MM-DD zijn");
          const amount = num(b.amount);
          if (amount === null) return fail(400, "amount moet een getal zijn");
          await store().setJSON(key, { date: b.date, amount, tpId: num(b.tpId) ?? Date.now() });
          return json({ ok: true, id });
        }
        const prev = (await store().get(key, { type: "json" })) as Record<string, unknown> | null;
        const doc = cleanTrade(b, prev);
        await store().setJSON(key, doc);
        return json({ ok: true, id, trade: doc });
      }
    }
    return fail(404, "Onbekende route.");
  } catch (e) {
    return fail(400, e instanceof Error ? e.message : "Ongeldig verzoek.");
  }
};

export const config: Config = { path: "/api/*" };
