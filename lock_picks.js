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
  /* Likely Scorers, the second goal list, lock the same way: started games keep their saved picks, the rest stay open. */
  let ls = (store[day] && store[day].ls) || [];
  if (api.likelyOk) {
    const keptL = ls.filter(k => k.ts && Date.parse(k.ts) <= now).map(k => Object.assign(k, { lk: 1 }));
    const haveL = new Set(keptL.map(k => k.p));
    const candL = api.slate(day).filter(r => api.likelyOk(r) && !haveL.has(r.p) && r.info.m.ts && Date.parse(r.info.m.ts) > now);
    const openL = api.likelySelect(candL, cap, keptL).map(r => ({ p: r.p, n: r.name, t: r.t, o: r.opp, ts: r.info.m.ts, price: r.px.price, book: r.px.book, nb: r.px.n,
      pr: +r.pr.toFixed(4), mk: +r.mk.toFixed(4), ps: +r.ps.toFixed(4), ev: +r.evS.toFixed(4), vs: +r.vsS.toFixed(2), pts: +(r.ps - 1 / r.pay).toFixed(4), lk: 0 }));
    ls = keptL.concat(openL).sort((a, b) => (b.pts || 0) - (a.pts || 0));
  }
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
  /* A record of every pick as it was first listed, and the last price on it before its game started. A pick that
     drops off the list before it locks stays here, so the Tracker can grade what was on offer earlier in the day and
     see whether the price then moved toward it (the "closing line"). */
  const stamp = new Date(now).toISOString(), prevDay = store[day] || {};
  const seenG = prevDay.seenG || {}, seenS = prevDay.seenS || {};
  const byP = {}; api.slate(day).forEach(r => { byP[r.p] = r; });
  picks.forEach(k => {
    if (!seenG[k.p]) seenG[k.p] = { p: k.p, n: k.n, t: k.t, o: k.o, ts: k.ts, price0: k.price, book0: k.book, at0: stamp };
    if (!k.lk) seenG[k.p].last = stamp;
  });
  Object.values(seenG).forEach(e => {
    if (!e.ts || Date.parse(e.ts) <= now) return;
    const r = byP[e.p];
    if (r && r.px) { e.close = r.px.price; e.closeBook = r.px.book; }
  });
  shots.forEach(k => {
    const key = k.p + "|" + k.side + "|" + k.line;
    if (!seenS[key]) seenS[key] = { p: k.p, n: k.n, t: k.t, o: k.o, ts: k.ts, side: k.side, line: k.line, price0: k.price, book0: k.book, at0: stamp };
    if (!k.lk) seenS[key].last = stamp;
  });
  Object.values(seenS).forEach(e => {
    if (!e.ts || Date.parse(e.ts) <= now) return;
    const r = byP[e.p], h = r && r.sh;
    if (!h) return;
    e.closeLine = h.line;
    const px = h.line === e.line ? (e.side === "o" ? h.o : h.u) : null;
    if (px) { e.close = px.price; e.closeBook = px.bk; }
  });
  /* Ladder candidates and the best alternate-line price on their next two rungs, as of the last run before each game
     starts. The alternate lines themselves are dropped from the price store after a couple of days, so this is what
     the Tracker's ladder record is graded on. */
  const lad = {};
  Object.entries(prevDay.lad || {}).forEach(([p, e]) => { if (e.ts && Date.parse(e.ts) <= now) lad[p] = e; });   // started: frozen
  if (api.altOf) Object.values(byP).forEach(r => {
    const h = r.sh, ts = r.info.m.ts;
    if (!h || r.xs == null || !ts || Date.parse(ts) <= now || r.xs < h.line + (api.ladderGap || 1)) return;
    const alt = api.altOf(r.info.o, r.name), a1 = alt[h.line + 1], a2 = alt[h.line + 2];
    lad[r.p] = { n: r.name, t: r.t, ts, line: h.line, xs: +r.xs.toFixed(3), r1: a1 ? [a1.price, a1.bk] : null, r2: a2 ? [a2.price, a2.bk] : null, at: stamp };
  });
  /* Rungs carrying the RUNG VALUE tag, as of the last run before each game starts, with the price they had. */
  let rv = (prevDay.rv || []).filter(e => e.ts && Date.parse(e.ts) <= now);      // started: frozen
  if (api.rungValues) Object.values(byP).forEach(r => {
    const ts = r.info.m.ts;
    if (!ts || Date.parse(ts) <= now) return;
    api.rungValues(r).forEach(v => rv.push({ p: r.p, n: r.name, t: r.t, ts, line: v.line, price: v.price, book: v.bk, pa: +v.pa.toFixed(4), pts: +v.pts.toFixed(4), at: stamp }));
  });
  /* Ladder Watch: each candidate's rungs, prices and stake split as of the last run before his game starts. */
  const seenL = prevDay.seenL || {};
  let lw = (prevDay.lw || []).filter(e => e.ts && Date.parse(e.ts) <= now);      // started: frozen
  if (api.ladderOf) {
    const open = [];
    Object.values(byP).forEach(r => {
      const ts = r.info.m.ts;
      if (!ts || Date.parse(ts) <= now) return;
      const L = api.ladderOf(r);
      if (L) open.push(L);
    });
    open.sort((x, y) => y.edge - x.edge).slice(0, Math.max(0, cap - lw.length)).forEach(L => {   // the nightly cap, after the places already frozen
      const r = L.r, c = {};
      L.cells.forEach(x => { if (x.a) c[x.need] = [x.a.price, x.a.bk, x.w || 0]; });
      const e = { p: r.p, n: r.name, t: r.t, ts: r.info.m.ts, line: L.line, xs: +r.xs.toFixed(3), c, at: stamp };
      lw.push(e);
      if (!seenL[r.p]) seenL[r.p] = Object.assign({}, e, { at0: stamp });   // the ladder as first listed, kept even if he later drops off
      seenL[r.p].last = stamp;
    });
  }
  /* Table numbers freeze at puck drop too: every skater's projection as of the last run before his game starts.
     Unstarted games are rewritten each run; a started game keeps what it had. The page reads these until the
     finished game arrives in the archive, so nothing moves while a game is on. */
  const fz = Object.assign({}, prevDay.fz || {});
  const r5 = v => typeof v === "number" && isFinite(v) && !Number.isInteger(v) ? +v.toPrecision(5) : v;   // whole numbers are ids and flags: left alone
  const fresh = {};
  Object.values(byP).forEach(r => {
    const ts = r.info.m.ts;
    if (!ts || Date.parse(ts) <= now) return;
    const sd = r.info.side[r.t], g = fresh[r.info.key] || (fresh[r.info.key] = { at: stamp, s: {} });
    const t = g.s[r.t] || (g.s[r.t] = { f: Object.fromEntries(Object.entries(sd.f).map(([k, v]) => [k, r5(v)])), scale: r5(sd.scale), model: r5(sd.model), mkt: r5(sd.mkt), gk: { p: sd.gk.p || 0, src: sd.gk.src || "" }, r: [] });
    const e = { p: r.p, l: r5(r.lam0), x: r5(r.xs), u: r.unit || 0, b: Object.fromEntries(Object.entries(r.b).map(([k, v]) => [k, r5(v)])),
      c: r5(r.pr), s: r5(r.ps != null ? r.ps : r.pr), px: r.px ? r.px.price : null, ln: r.sh ? r.sh.line : null };
    if (r.inj) e.inj = r.inj;
    if (r.back) e.back = 1;
    if (r.sh && r.xs != null && api.shotPrep) {                    // the shots market as it stands: line, the over's pick chance, the better side's edge, both best prices
      api.shotPrep(r, day);
      const v = r.sv || {};
      if (v.pso != null) e.q = [r.sh.line, r5(v.pso), v.best > -9 ? r5(v.best) : null, v.side || "", r.sh.o ? r.sh.o.price : null, r.sh.u ? r.sh.u.price : null];
    }
    t.r.push(e);
  });
  /* The day's change log: what moved for each skater between one refresh and the next, while his game is still to start.
     Each entry lists the inputs that changed and the net move in his chance to score and his expected shots. */
  const log = prevDay.log || {};
  const note = (pid, ev) => { const l = log[pid] || (log[pid] = []); l.push(ev); if (l.length > 14) l.splice(0, l.length - 14); };
  const nameOf = id => (id && D.players && D.players[id] ? D.players[id][0] : "not named");
  const pp = u => (u === 1 ? "PP1" : u === 2 ? "PP2" : "none");
  const od = v => (v == null ? "no price" : (v > 0 ? "+" : "") + v);
  const clock = sec => { sec = Math.round(sec); return Math.floor(sec / 60) + ":" + ("0" + (sec % 60)).slice(-2); };
  Object.entries(fresh).forEach(([key, g]) => {
    const old = (prevDay.fz || {})[key];
    if (!old) return;                                  // first sight of this game: nothing to compare with
    Object.entries(g.s).forEach(([t, cur]) => {
      const was = old.s && old.s[t];
      if (!was) return;
      const before = {}; was.r.forEach(e => { before[e.p] = e; });
      const team = [];
      if ((was.f.goalie || 0) !== (cur.f.goalie || 0)) team.push(["Opposing goalie", nameOf(was.f.goalie), nameOf(cur.f.goalie)]);
      if (was.mkt != null && cur.mkt != null && Math.abs(was.mkt - cur.mkt) >= 0.05) team.push(["Books' team goal total", was.mkt.toFixed(2), cur.mkt.toFixed(2)]);
      cur.r.forEach(e => {
        const b = before[e.p];
        if (!b) { note(e.p, { at: stamp, ch: [["Lineup", "not expected to dress", "expected to dress"]] }); return; }
        const ch = [];
        if ((b.u || 0) !== (e.u || 0)) ch.push(["Power play", pp(b.u), pp(e.u)]);
        if (b.b && e.b && Math.abs(b.b.mt - e.b.mt) >= 0.01) ch.push(["Linemates", b.b.mt.toFixed(2) + "×", e.b.mt.toFixed(2) + "×"]);
        if (b.b && e.b && Math.abs((b.b.tev + b.b.tpp) - (e.b.tev + e.b.tpp)) >= 20) ch.push(["Expected ice time", clock(b.b.tev + b.b.tpp), clock(e.b.tev + e.b.tpp)]);
        if (b.px !== undefined && b.px !== e.px) ch.push(["Best goal price", od(b.px), od(e.px)]);
        if (b.ln !== undefined && b.ln !== e.ln) ch.push(["Shots line", b.ln == null ? "none" : String(b.ln), e.ln == null ? "none" : String(e.ln)]);
        if (b.q && e.q) {
          if (b.q[4] !== e.q[4]) ch.push(["Best over price", od(b.q[4]), od(e.q[4])]);
          if (b.q[5] !== e.q[5]) ch.push(["Best under price", od(b.q[5]), od(e.q[5])]);
          if (b.q[0] === e.q[0] && b.q[4] === e.q[4] && b.q[5] === e.q[5] && Math.abs(b.q[1] - e.q[1]) >= 0.005) ch.push(["Books' over chance", (b.q[1] * 100).toFixed(1) + "%", (e.q[1] * 100).toFixed(1) + "%"]);   // other books moved while the best prices stood still
        }
        const i0 = b.inj ? b.inj.st : "", i1 = e.inj ? e.inj.st : "";
        if (i0 !== i1) ch.push(["Injury report", i0 || "not listed", i1 || "not listed"]);
        const all = ch.concat(team);
        const dc = b.s != null && e.s != null ? e.s - b.s : 0, dx = b.x != null && e.x != null ? e.x - b.x : 0;
        if (!all.length && Math.abs(dc) < 0.005 && Math.abs(dx) < 0.05) return;
        if (!all.length) all.push(["Team around him", "", "roles or lineup changed"]);
        const ev = { at: stamp, ch: all };
        if (b.s != null && e.s != null) ev.c = [+b.s.toFixed(4), +e.s.toFixed(4)];
        if (b.x != null && e.x != null) ev.x = [+b.x.toFixed(2), +e.x.toFixed(2)];
        const val = (ch2, price) => { if (ch2 == null || price == null) return null; const pay = price > 0 ? 1 + price / 100 : 1 + 100 / -price; return +(((ch2 * pay - 1) / (pay - 1)) * 100).toFixed(2); };
        const v0 = b.px !== undefined ? val(b.s, b.px) : null, v1 = val(e.s, e.px);
        if (v0 != null && v1 != null) ev.v = [v0, v1];                // the value score before and after, each at the price of its moment
        if (b.q && e.q) {                                               // shots: the over's chance and the better side's edge, before and after
          ev.so = [+b.q[1].toFixed(4), +e.q[1].toFixed(4)];
          if (b.q[2] != null && e.q[2] != null) { ev.se = [+b.q[2].toFixed(4), +e.q[2].toFixed(4)]; ev.ss = [b.q[3], e.q[3]]; }
          if (b.q[0] !== e.q[0]) ev.sl = [b.q[0], e.q[0]];
        }
        note(e.p, ev);
      });
      const nowIn = new Set(cur.r.map(e => e.p));
      was.r.forEach(e => { if (!nowIn.has(e.p)) note(e.p, { at: stamp, ch: [["Lineup", "expected to dress", "not expected to dress"]] }); });
    });
  });
  /* Joining or leaving one of the lists is part of the story too. */
  const lists = [["Nightly Picks", prevDay.picks, picks], ["Shot Picks", prevDay.shots, shots], ["Likely Scorers", prevDay.ls, ls], ["Ladder Watch", prevDay.lw, lw]];
  if (store[day]) lists.forEach(([lab, a, b]) => {
    const A = new Set((a || []).map(k => k.p)), B = new Set((b || []).map(k => k.p));
    B.forEach(pid => { if (!A.has(pid)) note(pid, { at: stamp, ch: [[lab, "", "added"]] }); });
    A.forEach(pid => { if (!B.has(pid)) note(pid, { at: stamp, ch: [[lab, "", "dropped"]] }); });
  });
  Object.assign(fz, fresh);
  if (picks.length || shots.length || ls.length || Object.keys(lad).length || rv.length || lw.length || Object.keys(fz).length || store[day]) {
    store[day] = { at: (store[day] && allStarted && store[day].at) || stamp, locked: anyStarted ? 1 : 0, done: allStarted ? 1 : 0, picks, shots, ls, seenG, seenS, seenL, lad, rv, lw, fz, log };
  }
}
Object.keys(store).forEach(k => { if (store[k] && k < day && Math.round((Date.parse(day) - Date.parse(k)) / 864e5) > 2) { delete store[k].fz; delete store[k].log; } });   // the archive has those games by now
const keep = Object.keys(store).sort().slice(-250);
store = Object.fromEntries(keep.map(k => [k, store[k]]));
fs.writeFileSync(storePath, JSON.stringify(store));
const doc = JSON.parse(raw);
doc.locks = store;
fs.writeFileSync(dataPath, JSON.stringify(doc));
const t = store[day];
console.log(`lock_picks: ${day} ${t ? (t.done ? "all locked" : t.locked ? "partly locked" : "open") + ", " + t.picks.length + " picks: " + t.picks.map(x => x.n + " " + (x.price > 0 ? "+" : "") + x.price + (x.lk ? " [locked]" : "")).join(", ") : "no picks"}`);
if (t && t.shots && t.shots.length) console.log(`lock_picks: shot picks: ${t.shots.map(x => x.n + " " + (x.side === "o" ? "over " : "under ") + x.line + " " + (x.price > 0 ? "+" : "") + x.price + (x.lk ? " [locked]" : "")).join(", ")}`);
