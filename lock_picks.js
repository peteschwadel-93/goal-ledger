#!/usr/bin/env node
/* Locks each night's Nightly Picks and Shot Picks.

   Run after build_data.py:   node lock_picks.js [index.html] [data.json] [picks.json]

   It runs the page's own model (the script inside index.html) on data.json, works out tonight's Nightly Picks exactly as
   the page would, and keeps them in picks.json. A pick is frozen when its own game starts; until then it can still
   change on each run. The store is also written into data.json as "locks" so the page and the Tracker show and grade
   the frozen list. */
const fs = require("fs");
const [html = "index.html", dataPath = "data.json", storePath = "picks.json"] = process.argv.slice(2);

const page = fs.readFileSync(html, "utf8");
const scripts = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const app = scripts[scripts.length - 1];
const raw = fs.readFileSync(dataPath, "utf8");

const els = {};
const el = () => ({ hidden: false, textContent: "", innerHTML: "", dataset: {}, addEventListener() {}, setAttribute() {} });
let api = null;
global.window = { __GL_HOOK: a => { api = a; }, scrollTo() {}, scrollY: 0 };
global.document = { getElementById: id => els[id] || (els[id] = el()), addEventListener() {}, querySelectorAll: () => [], activeElement: null, hidden: true };
els["goal-data"] = { textContent: raw };
global.location = { protocol: "node:", hostname: "", hash: "" };
global.localStorage = { getItem: () => null, setItem() {} };
global.history = { replaceState() {} };
(0, eval)(app);
if (!api) { console.error("lock_picks: the page did not expose its model; nothing locked"); process.exit(0); }

const D = api.data(), S = api.S;
S.minP = "np"; S.mkt = 1; S.book = ""; S.pos = "all";
let store = {};
try { store = JSON.parse(fs.readFileSync(storePath, "utf8")); } catch (_) {}

const day = api.todayET(), now = Date.now();
const games = (D.sched || []).filter(m => m.d === day);
if (games.length) {
  /* Each pick locks when its own game starts, so a noon game does not freeze the evening slate.
     - A pick already saved whose game has started is kept exactly as it was: it holds its place for good.
     - The remaining places go to the best candidates from games that have not started; these can still change.
     - A player whose game has started and who was not on the saved list can no longer become a pick. */
  const cap = api.npCap(api.matchups(day).length);
  const prev = (store[day] && store[day].picks) || [];
  const kept = prev.filter(k => k.ts && Date.parse(k.ts) <= now).map(k => Object.assign(k, { lk: 1 }));
  const taken = new Set(kept.map(k => k.p));
  const open = api.slate(day)
    .filter(r => r.np && !taken.has(r.p) && r.info.m.ts && Date.parse(r.info.m.ts) > now)
    .sort((a, b) => b.vsS - a.vsS).slice(0, Math.max(0, cap - kept.length))
    .map(r => ({ p: r.p, n: r.name, t: r.t, o: r.opp, ts: r.info.m.ts, price: r.px.price, book: r.px.book, nb: r.px.n,
      pr: +r.pr.toFixed(4), mk: +r.mk.toFixed(4), ps: +r.ps.toFixed(4), ev: +r.evS.toFixed(4), vs: +r.vsS.toFixed(2), lk: 0 }));
  const picks = kept.concat(open).sort((a, b) => b.vs - a.vs);
  const anyStarted = games.some(m => Date.parse(m.ts) <= now), allStarted = games.every(m => Date.parse(m.ts) <= now);
  /* Shot Picks lock the same way: a pick whose game has started is kept as saved; the other places stay open. */
  let shots = (store[day] && store[day].shots) || [];
  if (api.shotCands) {
    const rows = api.slate(day);
    const keptS = shots.filter(k => k.ts && Date.parse(k.ts) <= now).map(k => Object.assign(k, { lk: 1 }));
    const have = new Set(keptS.map(k => k.p));
    const cands = api.shotCands(day, rows).filter(c => !have.has(c.p) && c.ts && Date.parse(c.ts) > now);
    const openS = api.shotSelect(cands, cap, keptS).map(c => ({ p: c.p, n: c.r.name, t: c.r.t, o: c.r.opp, ts: c.ts, side: c.side, line: c.line,
      price: c.price, book: c.book, pm: +c.pm.toFixed(4), mk: +c.mk.toFixed(4), ps: +c.ps.toFixed(4), ev: +c.ev.toFixed(4), vs: +c.vs.toFixed(4), xs: +c.xs.toFixed(3), lk: 0 }));
    shots = keptS.concat(openS).sort((x, y) => (y.vs || 0) - (x.vs || 0));
  }
  if (picks.length || shots.length || store[day]) {
    store[day] = { at: (store[day] && allStarted && store[day].at) || new Date(now).toISOString(), locked: anyStarted ? 1 : 0, done: allStarted ? 1 : 0, picks, shots };
  }
}
const keep = Object.keys(store).sort().slice(-250);
store = Object.fromEntries(keep.map(k => [k, store[k]]));
fs.writeFileSync(storePath, JSON.stringify(store));
const doc = JSON.parse(raw);
doc.locks = store;
fs.writeFileSync(dataPath, JSON.stringify(doc));
const t = store[day];
console.log(`lock_picks: ${day} ${t ? (t.done ? "all locked" : t.locked ? "partly locked" : "open") + ", " + t.picks.length + " picks: " + t.picks.map(x => x.n + " " + (x.price > 0 ? "+" : "") + x.price + (x.lk ? " [locked]" : "")).join(", ") : "no picks"}`);
if (t && t.shots && t.shots.length) console.log(`lock_picks: shot picks: ${t.shots.map(x => x.n + " " + (x.side === "o" ? "over " : "under ") + x.line + " " + (x.price > 0 ? "+" : "") + x.price + (x.lk ? " [locked]" : "")).join(", ")}`);
