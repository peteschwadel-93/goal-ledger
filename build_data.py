#!/usr/bin/env python3
"""Build the data behind the Goal Ledger dashboard (NHL anytime goalscorers).

  python3 build_data.py --install
      One-time setup. After this the dashboard lives at http://localhost:8766,
      starts by itself whenever you log in, and refreshes on its own every two
      hours. Bookmark the address. Undo with --uninstall.

  python3 build_data.py --serve
      Opens the dashboard at http://localhost:8766 with a working Refresh
      button. Keep goal_ledger.html in the same folder as this script.

  python3 build_data.py --out data.json [--html goal_ledger.html]
      Build once and write the data (and, with --html, put it inside the page).

Where the numbers come from
  Games     The sportsdataverse archive of NHL play-by-play, box scores and
            shift charts on GitHub (fastRhockey-nhl-data). It is rebuilt from
            the NHL's own feeds every morning around 4am ET, so last night's
            games are in by the time tonight's slate matters. Season files are
            kept in ./nhl_cache; finished seasons are downloaded once.
  Injuries  ESPN's NHL injury feed.
  Prices    The Odds API: anytime-goalscorer prices per game, plus moneyline and
            total for every game on the slate. Put your key in the ODDS_API_KEY
            environment variable (or in a file called odds_key.txt next to this
            script). Each look at a game costs one request; ODDS_LOOKS sets how
            many looks and when. See remember_odds() for the budget.

--seasons N keeps the N most recent seasons (default 3). A season is named by
the year it starts: 2026 = 2026-27.
"""
import argparse, io, json, os, re, shutil, socket, subprocess, sys, threading, time, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

try:
    import numpy as np
    import pandas as pd
    import pyarrow  # noqa: F401  (pandas needs it to read the archive)
except ImportError:  # --install adds them
    pd = None

ARCHIVE = "https://raw.githubusercontent.com/sportsdataverse/fastRhockey-nhl-data/main/nhl/"
FILES = {"pbp": "pbp_lite/parquet/play_by_play_lite_{y}.parquet", "skaters": "skater_box/parquet/skater_box_{y}.parquet",
         "goalies": "goalie_box/parquet/goalie_box_{y}.parquet", "shifts": "shifts/parquet/shifts_{y}.parquet",
         "sched": "schedules/parquet/nhl_schedule_{y}.parquet"}
PBP_COLS = ["game_id", "game_date", "season_type", "home_abbr", "away_abbr", "event_idx", "event_type", "event_team_type",
            "period_type", "game_seconds", "event_player_1_id", "event_player_1_name", "event_player_1_type",
            "event_player_2_id", "event_player_2_name", "event_player_2_type", "event_player_3_id", "event_player_3_name",
            "event_goalie_id", "event_goalie_name", "empty_net", "home_skaters", "away_skaters", "home_goalie_in",
            "away_goalie_in", "home_goalie_id", "away_goalie_id", "shot_distance", "shot_angle", "secondary_type", "xg"]
ESPN_INJ = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"
ESPN_ABBR = {"TB": "TBL", "SJ": "SJS", "NJ": "NJD", "LA": "LAK", "UTAH": "UTA", "VGS": "VGK", "WAS": "WSH", "MON": "MTL",
             "CLB": "CBJ", "NAS": "NSH", "WIN": "WPG", "CAL": "CGY"}
UA = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
CACHE = "nhl_cache"
ET = ZoneInfo("America/New_York")
HD_XG = 0.15   # an unblocked attempt at or above this goal chance counts as high-danger

# Fallback shot quality for the few attempts the archive leaves unrated: a logistic curve on distance,
# angle, shot type and manpower, fitted to 2023-24 and 2024-25 unblocked attempts at a tended net.
XG_B = {"k": -0.2414, "d": -0.0554, "ld": -0.1186, "ang": -0.0173, "pp": 0.2658, "sh": 0.0867,
        "snap": 0.4088, "slap": 0.5245, "tip-in": -0.8065, "backhand": -0.4618, "deflected": -0.7009,
        "wrap-around": -0.8952, "bat": -0.2396, "poke": -0.3976}


def ssl_context():
    """Python from python.org on a Mac ships without root certificates; use certifi's or the system's."""
    import ssl
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        for path in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(path):
                try:
                    ctx.load_verify_locations(cafile=path)
                except Exception:
                    pass
    return ctx


CTX = None


def get(url, timeout=120, headers=None):
    """Returns (body, response headers)."""
    global CTX
    CTX = CTX or ssl_context()
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read(), r.headers


# ---------- archive ----------
def current_season():
    now = datetime.now(ET)
    return now.year if now.month >= 9 else now.year - 1


def label(yr):
    return f"{yr}-{str(yr + 1)[2:]}"


def fetch_file(kind, season, fresh):
    """One archive file for a season, from ./nhl_cache unless it should be downloaded again."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{kind}_{season}.parquet")
    stale = fresh and (not os.path.exists(path) or time.time() - os.path.getmtime(path) > 3 * 3600)
    if stale or not os.path.exists(path):
        try:
            raw, _ = get(ARCHIVE + FILES[kind].format(y=season + 1), timeout=600)
            with open(path + ".part", "wb") as f:
                f.write(raw)
            os.replace(path + ".part", path)
        except Exception as e:
            if not os.path.exists(path):
                raise RuntimeError(f"No {kind} file for {label(season)} ({e})")
            print(f"kept the stored {kind} file for {label(season)}: {e}", file=sys.stderr)
    return path


def load_season(season, fresh):
    f = {k: fetch_file(k, season, fresh) for k in FILES}
    pbp = pd.read_parquet(f["pbp"], columns=PBP_COLS)
    return {"pbp": pbp, "skaters": pd.read_parquet(f["skaters"]), "goalies": pd.read_parquet(f["goalies"]),
            "shifts": pd.read_parquet(f["shifts"], columns=["game_id", "game_seconds", "ids_on", "ids_off"]),
            "sched": pd.read_parquet(f["sched"])}


# ---------- transform ----------
def toi_s(v):
    try:
        m, s = str(v).split(":")
        return int(m) * 60 + int(s)
    except Exception:
        return 0


def ids(v):
    return [int(x) for x in str(v).split(",") if x.strip().isdigit() and int(x) > 0]


def fallback_xg(dist, ang, kind, pp, sh):
    d = min(max(float(dist), 0.0), 90.0)
    z = (XG_B["k"] + XG_B["d"] * d + XG_B["ld"] * np.log(max(d, 1.0)) + XG_B["ang"] * min(abs(float(ang)), 90.0)
         + XG_B["pp"] * pp + XG_B["sh"] * sh + XG_B.get(kind, 0.0))
    return 1 / (1 + np.exp(-z))


def on_ice(shifts, team_of, goalies, end):
    """Per-player seconds on the power play, and each team's forward lines, from the shift-change records.

    Returns ({player: pp seconds}, {team: pp seconds}, presence {player: 0/1 per second}, masks) .
    A second counts as a power play for a team when it has more skaters on than the other side,
    its own goalie is in net and the other side has at least three skaters.
    """
    n = end + 2
    diff = {}
    for sec, on, off in zip(shifts.game_seconds.values, shifts.ids_on.values, shifts.ids_off.values):
        s = min(int(sec), n - 1)
        for p in ids(on):
            diff.setdefault(p, np.zeros(n, dtype=np.int16))[s] += 1
        for p in ids(off):
            diff.setdefault(p, np.zeros(n, dtype=np.int16))[s] -= 1
    pres = {p: (np.cumsum(d)[:end] > 0) for p, d in diff.items() if p in team_of}
    teams = sorted(set(team_of.values()))
    if len(teams) != 2:
        return {}, {}, pres, {}
    sk = {t: np.zeros(end, dtype=np.int16) for t in teams}
    gk = {t: np.zeros(end, dtype=np.int16) for t in teams}
    for p, a in pres.items():
        (gk if p in goalies else sk)[team_of[p]] += a
    a, b = teams
    mask = {a: (sk[a] > sk[b]) & (gk[a] > 0) & (sk[b] >= 3), b: (sk[b] > sk[a]) & (gk[b] > 0) & (sk[a] >= 3)}
    five = (sk[a] == 5) & (sk[b] == 5)
    pp = {p: int((arr & mask[team_of[p]]).sum()) for p, arr in pres.items() if p not in goalies}
    return pp, {t: int(m.sum()) for t, m in mask.items()}, pres, {"five": five}


def lines(pres, five, fwd):
    """Forward trios by shared five-on-five ice time: the busiest forward and his two most common partners, and so on."""
    fwd = [p for p in fwd if p in pres]
    if len(fwd) < 3 or five is None:
        return []
    m = np.array([pres[p] & five for p in fwd], dtype=np.int32)
    share = m @ m.T
    left = list(range(len(fwd)))
    out = []
    while len(left) >= 3 and len(out) < 4:
        lead = max(left, key=lambda i: share[i, i])
        mates = sorted((i for i in left if i != lead), key=lambda i: -share[lead, i])[:2]
        trio = [lead] + mates
        out.append([fwd[i] for i in trio])
        left = [i for i in left if i not in trio]
    return out


def build_season(season, d):
    pbp = d["pbp"]
    pbp = pbp[pbp.period_type != "SHOOTOUT"].sort_values(["game_id", "event_idx"], kind="stable")
    names = {}
    for i in ("1", "2", "3"):
        sub = pbp[[f"event_player_{i}_id", f"event_player_{i}_name"]].dropna().drop_duplicates(f"event_player_{i}_id")
        names.update({int(a): b for a, b in sub.values})
    sub = pbp[["event_goalie_id", "event_goalie_name"]].dropna().drop_duplicates("event_goalie_id")
    names.update({int(a): b for a, b in sub.values})
    pos = {}
    by_game = {k: v for k, v in pbp.groupby("game_id", sort=False)}
    sk_by = {k: v for k, v in d["skaters"].groupby("game_id", sort=False)}
    gl_by = {k: v for k, v in d["goalies"].groupby("game_id", sort=False)}
    sh_by = {k: v for k, v in d["shifts"].groupby("game_id", sort=False)}
    games = []
    for gid in sorted(by_game):
        g, sk, gl = by_game[gid], sk_by.get(gid), gl_by.get(gid)
        if sk is None or gl is None or str(gid)[4:6] not in ("02", "03"):
            continue
        home, away = g.home_abbr.iloc[0], g.away_abbr.iloc[0]
        side = {"home": home, "away": away}
        team_of, goalies = {}, set()
        for r in sk.itertuples():
            team_of[int(r.player_id)] = r.team_abbrev
            pos[int(r.player_id)] = r.position
            names.setdefault(int(r.player_id), r.player_name)
        for r in gl.itertuples():
            team_of[int(r.player_id)] = r.team_abbrev
            goalies.add(int(r.player_id))
            pos[int(r.player_id)] = "G"
            names.setdefault(int(r.player_id), r.player_name)
        end = int(g.game_seconds.max())
        pp, team_pp, pres, masks = ({}, {}, {}, {})
        if gid in sh_by and end > 0:
            pp, team_pp, pres, masks = on_ice(sh_by[gid], team_of, goalies, end)
        # shots: every unblocked attempt, with the manpower at that moment
        P = {}   # player -> [fen, ixg_other, ixg_pp, eng, a1, hd]
        T = {t: {"fen": 0, "xg": 0.0, "ppxg": 0.0, "eng": 0} for t in (home, away)}
        GK = {}  # goalie -> [shots on goal faced, goals, xg faced]
        ev = g[g.event_type.isin(["SHOT", "MISSED_SHOT", "GOAL"])]
        for r in ev.itertuples():
            t = side.get(r.event_team_type)
            if t is None or pd.isna(r.event_player_1_id):
                continue
            p = int(r.event_player_1_id)
            hs, as_ = int(r.home_skaters), int(r.away_skaters)
            own, opp = (hs, as_) if r.event_team_type == "home" else (as_, hs)
            opp_g = r.away_goalie_in if r.event_team_type == "home" else r.home_goalie_in
            en = bool(r.empty_net) if pd.notna(r.empty_net) else False
            en = en or opp_g == 0
            goal = r.event_type == "GOAL"
            rec = P.setdefault(p, [0, 0.0, 0.0, 0, 0, 0])
            if goal and pd.notna(r.event_player_2_id) and r.event_player_2_type == "Assist":
                P.setdefault(int(r.event_player_2_id), [0, 0.0, 0.0, 0, 0, 0])[4] += 1
            if en:   # shooting at an empty net says nothing about shot quality; only the goal is kept
                if goal:
                    rec[3] += 1
                    T[t]["eng"] += 1
                continue
            is_pp, is_sh = own > opp, own < opp
            x = r.xg
            if pd.isna(x):
                x = fallback_xg(r.shot_distance if pd.notna(r.shot_distance) else 35, r.shot_angle if pd.notna(r.shot_angle) else 30,
                                r.secondary_type, int(is_pp), int(is_sh))
            x = float(x)
            rec[0] += 1
            rec[2 if is_pp else 1] += x
            rec[5] += 1 if x >= HD_XG else 0
            T[t]["fen"] += 1
            T[t]["xg"] += x
            if is_pp:
                T[t]["ppxg"] += x
            gk = r.event_goalie_id if pd.notna(r.event_goalie_id) else (r.away_goalie_id if r.event_team_type == "home" else r.home_goalie_id)
            if pd.notna(gk):
                k = GK.setdefault(int(gk), [0, 0, 0.0])
                k[2] += x
                if r.event_type != "MISSED_SHOT":
                    k[0] += 1
                if goal:
                    k[1] += 1
        rows = {home: [], away: []}
        tm = {}
        for r in sk.itertuples():
            p = int(r.player_id)
            e = P.get(p, [0, 0.0, 0.0, 0, 0, 0])
            toi = toi_s(r.toi)
            if toi <= 0:
                continue
            rows[r.team_abbrev].append([p, int(r.goals), int(r.shots_on_goal), e[0], round(e[1] * 1000), round(e[2] * 1000), toi,
                                        min(pp.get(p, 0), toi), int(r.power_play_goals), e[3], e[4], e[5]])
        for t in (home, away):
            rows[t].sort(key=lambda x: -x[6])
            tb = sk[sk.team_abbrev == t]
            tm[t] = [int(tb.goals.sum()), int(tb.shots_on_goal.sum()), T[t]["fen"], round(T[t]["xg"] * 100), team_pp.get(t, 0),
                     round(T[t]["ppxg"] * 100), int(tb.power_play_goals.sum()), T[t]["eng"]]
        gks = {home: [], away: []}
        for r in gl.itertuples():
            p = int(r.player_id)
            k = GK.get(p, [0, 0, 0.0])
            if toi_s(r.toi) <= 0:
                continue
            gks[r.team_abbrev].append([p, k[0], k[1], round(k[2] * 100), 1 if bool(r.starter) else 0, toi_s(r.toi)])
        for t in gks:
            gks[t].sort(key=lambda x: (-x[4], -x[5]))
        ln = {}
        for t in (home, away):
            fwd = [x[0] for x in rows[t] if pos.get(x[0]) in ("C", "L", "R")]
            ln[t] = lines(pres, masks.get("five"), fwd)
        fin = g[g.event_type == "GOAL"]
        hg = int((fin.event_team_type == "home").sum())
        ag = int((fin.event_team_type == "away").sum())
        games.append({"id": int(gid), "d": str(g.game_date.iloc[0])[:10], "s": season, "po": 1 if str(gid)[4:6] == "03" else 0,
                      "h": home, "a": away, "sc": [ag, hg], "ot": 1 if end > 3600 else 0, "p": rows, "t": tm, "g": gks, "l": ln})
    return games, names, pos


def upcoming(sched, days=7):
    """Games not yet final over the next week: [{d, t, ts, h, a, gid}], in Eastern time."""
    today = datetime.now(ET).date()
    out = []
    for r in sched.itertuples():
        if str(r.game_state) in ("OFF", "FINAL") or str(r.game_type) not in ("R", "P"):
            continue
        try:
            when = pd.to_datetime(r.game_time, utc=True).tz_convert(ET)
        except Exception:
            continue
        ahead = (when.date() - today).days
        if 0 <= ahead <= days:
            hour = when.strftime("%I:%M %p ET").lstrip("0")
            out.append({"d": when.strftime("%Y-%m-%d"), "t": hour, "ts": when.isoformat(), "h": r.home_team_abbr,
                        "a": r.away_team_abbr, "gid": int(r.game_id)})
    out.sort(key=lambda x: (x["ts"], x["gid"]))
    return out


# ---------- injuries ----------
def load_injuries():
    """Current injury designations by team from ESPN, or None if the feed cannot be read."""
    try:
        data = json.loads(get(ESPN_INJ)[0])
        out = {}
        for team in data.get("injuries", []):
            for it in team.get("injuries", []):
                ath = it.get("athlete") or {}
                ab = (ath.get("team") or {}).get("abbreviation")
                full = ath.get("displayName") or ""
                if not ab or not full:
                    continue
                out.setdefault(ESPN_ABBR.get(ab, ab), []).append({
                    "f": full, "st": it.get("status") or "", "ty": (it.get("details") or {}).get("type") or "",
                    "c": (it.get("shortComment") or "")[:200], "d": (it.get("date") or "")[:10]})
        return out
    except Exception as e:
        print(f"injury feed unavailable: {e}", file=sys.stderr)
        return None


# ---------- prices ----------
ODDS = "odds.json"
FLAGS = "flags.json"
ODDS_API = "https://api.the-odds-api.com/v4/sports/icehockey_nhl"
TEAM_NAMES = {"Anaheim Ducks": "ANA", "Boston Bruins": "BOS", "Buffalo Sabres": "BUF", "Calgary Flames": "CGY", "Carolina Hurricanes": "CAR",
              "Chicago Blackhawks": "CHI", "Colorado Avalanche": "COL", "Columbus Blue Jackets": "CBJ", "Dallas Stars": "DAL",
              "Detroit Red Wings": "DET", "Edmonton Oilers": "EDM", "Florida Panthers": "FLA", "Los Angeles Kings": "LAK",
              "Minnesota Wild": "MIN", "Montreal Canadiens": "MTL", "Montréal Canadiens": "MTL", "Nashville Predators": "NSH",
              "New Jersey Devils": "NJD", "New York Islanders": "NYI", "New York Rangers": "NYR", "Ottawa Senators": "OTT",
              "Philadelphia Flyers": "PHI", "Pittsburgh Penguins": "PIT", "San Jose Sharks": "SJS", "Seattle Kraken": "SEA",
              "St Louis Blues": "STL", "St. Louis Blues": "STL", "Tampa Bay Lightning": "TBL", "Toronto Maple Leafs": "TOR",
              "Utah Mammoth": "UTA", "Utah Hockey Club": "UTA", "Vancouver Canucks": "VAN", "Vegas Golden Knights": "VGK",
              "Washington Capitals": "WSH", "Winnipeg Jets": "WPG"}


def odds_key():
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        try:
            key = open("odds_key.txt", encoding="utf-8").read().strip()
        except Exception:
            key = ""
    return key


def implied(price):
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def parse_scorers(doc):
    """Anytime-goalscorer prices per player: [[player, best price, book, books quoting, median price], ...]."""
    seen = {}
    for bk in doc.get("bookmakers") or []:
        for mk in bk.get("markets") or []:
            if mk.get("key") != "player_goal_scorer_anytime":
                continue
            for o in mk.get("outcomes") or []:
                who = o.get("description") or o.get("name")
                price = o.get("price")
                if not who or who in ("Yes", "No") or str(o.get("name")) == "No" or not isinstance(price, (int, float)):
                    continue
                seen.setdefault(who, []).append((int(price), bk.get("title") or bk.get("key") or ""))
    out = []
    for who, quotes in seen.items():
        quotes.sort(key=lambda q: implied(q[0]))            # cheapest implied chance first = best payout
        mid = sorted(quotes, key=lambda q: implied(q[0]))[len(quotes) // 2][0]
        out.append([who, quotes[0][0], quotes[0][1], len(quotes), mid])
    return sorted(out, key=lambda x: implied(x[1]), reverse=True)


def parse_lines(events):
    """Moneyline and total per game from the slate call: {(away, home): {ml: [away, home], tot: [line, over, under], n}}."""
    out = {}
    for e in events:
        a, h = TEAM_NAMES.get(e.get("away_team")), TEAM_NAMES.get(e.get("home_team"))
        if not a or not h:
            continue
        ml_a, ml_h, tots = [], [], []
        for bk in e.get("bookmakers") or []:
            for mk in bk.get("markets") or []:
                oc = mk.get("outcomes") or []
                if mk.get("key") == "h2h":
                    pa = [o["price"] for o in oc if o.get("name") == e.get("away_team")]
                    ph = [o["price"] for o in oc if o.get("name") == e.get("home_team")]
                    if pa and ph:
                        ml_a.append(pa[0]); ml_h.append(ph[0])
                elif mk.get("key") == "totals":
                    ov = [o for o in oc if o.get("name") == "Over"]
                    un = [o for o in oc if o.get("name") == "Under"]
                    if ov and un and ov[0].get("point") is not None:
                        tots.append((float(ov[0]["point"]), int(ov[0]["price"]), int(un[0]["price"])))
        rec = {"n": len(ml_a), "id": e.get("id"), "ts": e.get("commence_time")}
        if ml_a:
            pa = sorted(implied(x) for x in ml_a)[len(ml_a) // 2]
            ph = sorted(implied(x) for x in ml_h)[len(ml_h) // 2]
            rec["wp"] = round(ph / (pa + ph), 4)            # home win chance with the bookmaker margin removed
            rec["ml"] = [int(sorted(ml_a)[len(ml_a) // 2]), int(sorted(ml_h)[len(ml_h) // 2])]
        if tots:
            pts = sorted(t[0] for t in tots)
            line = pts[len(pts) // 2]                       # the most central line, and the prices quoted on it
            at = [t for t in tots if t[0] == line]
            po = sorted(implied(t[1]) for t in at)[len(at) // 2]
            pu = sorted(implied(t[2]) for t in at)[len(at) // 2]
            rec["tot"] = [line, round(po / (po + pu), 4)]    # line, and the chance of the over with the margin removed
        out[(a, h)] = rec
    return out


def remember_odds(sched, old):
    """Prices for upcoming games from The Odds API, kept in odds.json.

    ODDS_LOOKS sets when each game is priced, as hours before puck drop (default "9,1.5"): a game gets one
    request the first time a refresh finds it inside each mark, so "24,2" prices tomorrow's games a day ahead
    and looks once more two hours out, and "6" looks once. Each look costs one request per game. Moneylines
    and totals for every listed game cost two requests a call and are fetched at most twice a day. A game with
    no prices posted yet is asked again no sooner than two hours later. Fetching stops when fewer than 15
    requests remain on the key.
    """
    store = dict((old or {}).get("odds") or {})
    try:
        with open(ODDS, encoding="utf-8") as f:
            store.update(json.load(f))
    except Exception:
        pass
    key = odds_key()
    if not key:
        return store
    before = json.dumps(store, sort_keys=True)
    now = datetime.now(ET)
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    try:
        looks = sorted((float(x) for x in os.environ.get("ODDS_LOOKS", "9,1.5").split(",") if x.strip()), reverse=True)
    except ValueError:
        looks = [9.0, 1.5]
    want, final = [], False
    for u in sched:
        hrs = (datetime.fromisoformat(u["ts"]) - now).total_seconds() / 3600
        if hrs <= 0:
            continue
        k = f"{u['d']}|{u['a']}|{u['h']}"
        rec = store.get(k) or {}
        due = sum(1 for x in looks if hrs <= x)          # looks this game should have had by now
        if rec.get("n", 0) >= due:
            continue
        if rec.get("tried") and (now - datetime.fromisoformat(rec["tried"]).replace(tzinfo=ET)).total_seconds() < 2 * 3600:
            continue
        want.append((k, u, due))
        final = final or due == len(looks)
    if want:
        try:
            day_key = "lines|" + now.strftime("%Y-%m-%d")
            meta = store.get(day_key) or {}
            if not meta.get("n") or (meta["n"] < 2 and final and meta.get("at", "") <= (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")):
                raw, hd = get(f"{ODDS_API}/odds?apiKey={key}&regions=us&markets=h2h,totals&oddsFormat=american", headers={"Accept": "application/json"})
                store[day_key] = {"n": meta.get("n", 0) + 1, "at": stamp}
                for (a, h), rec in parse_lines(json.loads(raw)).items():
                    when = pd.to_datetime(rec.pop("ts"), utc=True).tz_convert(ET).strftime("%Y-%m-%d") if rec.get("ts") else None
                    if not when:
                        continue
                    k = f"{when}|{a}|{h}"
                    cur = store.get(k) or {}
                    cur.update({"ln": {x: rec[x] for x in ("wp", "ml", "tot", "n") if x in rec}, "eid": rec.get("id"), "lat": stamp})
                    store[k] = cur
            events = None
            for k, u, due in want:
                rec = store.get(k) or {}
                eid = rec.get("eid")
                if not eid:   # listing events is free
                    if events is None:
                        events = json.loads(get(f"{ODDS_API}/events?apiKey={key}", headers={"Accept": "application/json"})[0])
                    for e in events:
                        if (TEAM_NAMES.get(e.get("away_team")), TEAM_NAMES.get(e.get("home_team"))) == (u["a"], u["h"]) and \
                                pd.to_datetime(e.get("commence_time"), utc=True).tz_convert(ET).strftime("%Y-%m-%d") == u["d"]:
                            eid = e["id"]
                    if not eid:
                        rec["tried"] = stamp
                        store[k] = rec
                        continue
                raw, hd = get(f"{ODDS_API}/events/{eid}/odds?apiKey={key}&regions=us&markets=player_goal_scorer_anytime&oddsFormat=american",
                              headers={"Accept": "application/json"})
                prices = parse_scorers(json.loads(raw))
                left = hd.get("x-requests-remaining")
                if prices:
                    if rec.get("p") and not rec.get("open"):
                        rec["open"] = rec["p"]            # keep the first look so the page can show how prices moved
                    rec.update({"p": prices, "n": due, "at": stamp, "eid": eid})   # a game first seen late skips the looks it missed
                    rec.pop("tried", None)
                else:
                    rec["tried"] = stamp
                store[k] = rec
                if left is not None:
                    store["left"] = left
                    if float(left) < 15:
                        print("odds: request allowance nearly used; stopping", file=sys.stderr)
                        break
        except Exception as e:
            print(f"odds unavailable: {str(e).replace(key, '***')}", file=sys.stderr)
    cutoff = (now - timedelta(days=120)).strftime("%Y-%m-%d")
    store = {k: v for k, v in store.items() if k == "left" or (k.split("|")[1] if k.startswith("lines|") else k[:10]) >= cutoff}
    if json.dumps(store, sort_keys=True) != before:
        with open(ODDS, "w", encoding="utf-8") as f:
            json.dump(store, f, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return store


def remember_flags(sched, inj, old):
    """Keep the injury designations seen before each of today's games, so the Tracker can replay what was known."""
    flags = dict((old or {}).get("flags") or {})
    try:
        with open(FLAGS, encoding="utf-8") as f:
            flags.update(json.load(f))
    except Exception:
        pass
    before = json.dumps(flags, sort_keys=True)
    now = datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    for u in sched:
        if u["d"] != today or inj is None or datetime.fromisoformat(u["ts"]) <= now:
            continue
        key = f"{u['d']}|{u['a']}|{u['h']}"
        flags[key] = {"inj": {t: [[x["f"], x["st"]] for x in inj.get(t, [])] for t in (u["a"], u["h"]) if inj.get(t)}}
    cutoff = (now - timedelta(days=120)).strftime("%Y-%m-%d")
    flags = {k: v for k, v in flags.items() if k[:10] >= cutoff}
    if json.dumps(flags, sort_keys=True) != before:
        with open(FLAGS, "w", encoding="utf-8") as f:
            json.dump(flags, f, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return flags


# ---------- assemble ----------
def embedded(path):
    try:
        page = open(path, encoding="utf-8").read()
        m = re.search(r'<script id="goal-data" type="application/json">(.*?)</script>', page, re.S)
        return json.loads(m.group(1))
    except Exception:
        return None


def make(seasons=3, season=None, html=None, live=True):
    yr = season or current_season()
    games, names, pos, sched, notes = [], {}, {}, [], []
    old = embedded(html) if html else None
    for y in range(yr - seasons + 1, yr + 1):
        try:
            d = load_season(y, fresh=(y == yr))
        except RuntimeError as e:
            notes.append(str(e))
            continue
        gs, nm, ps = build_season(y, d)
        games += gs
        for p, n in nm.items():   # a box score only gives "J. Smith"; keep a full name once one has been seen
            if p not in names or re.match(r"^\w\. ", names[p]) or not re.match(r"^\w\. ", n):
                names[p] = n
        pos.update(ps)
        if y == yr:
            sched = upcoming(d["sched"])
    if not games:
        raise RuntimeError("; ".join(notes) or "No games found")
    games.sort(key=lambda x: (x["d"], x["id"]))
    used = set()
    for g in games:
        for t in g["p"]:
            used.update(r[0] for r in g["p"][t])
            used.update(r[0] for r in g["g"][t])
    ys = sorted({g["s"] for g in games})
    out = {"season": " + ".join(label(y) for y in ys), "seasons": ys, "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
           "through": games[-1]["d"], "sched": sched,
           "players": {str(p): [names.get(p, f"#{p}"), pos.get(p, "")] for p in sorted(used)}, "games": games}
    if live:
        inj = load_injuries()
        if inj is not None:
            out["inj"] = inj
        out["flags"] = remember_flags(sched, inj, old)
        out["odds"] = remember_odds(sched, old)
        out["hasKey"] = 1 if odds_key() else 0
    if notes:
        out["note"] = "; ".join(notes)
    return out


def write_html(path, out):
    page = open(path, encoding="utf-8").read()
    blob = json.dumps(out, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    pat = re.compile(r'(<script id="goal-data" type="application/json">).*?(</script>)', re.S)
    if not pat.search(page):
        raise RuntimeError(f"{path} has no goal-data block to refresh")
    with open(path, "w", encoding="utf-8") as f:
        f.write(pat.sub(lambda m: m.group(1) + blob + m.group(2), page, count=1))


def serve(a):
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    html = a.html or "goal_ledger.html"
    if not os.path.exists(html):
        sys.exit(f"Put {html} in the same folder as this script, then run it again.")
    lock = threading.Lock()

    def refresh():
        out = make(a.seasons, a.season, html)
        write_html(html, out)
        print(f"{datetime.now(ET):%b %d %H:%M} refreshed: {len(out['games'])} games through {out['through']}, "
              f"{len(out['sched'])} scheduled", flush=True)
        return out

    def keep_fresh():
        while True:
            with lock:
                try:
                    refresh()
                except Exception as e:
                    print(f"{datetime.now(ET):%b %d %H:%M} refresh failed: {e}", file=sys.stderr, flush=True)
            time.sleep(max(a.every, 0.25) * 3600)

    class H(BaseHTTPRequestHandler):
        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0].split("#")[0] in ("/", "/index.html"):
                self.send(200, open(html, "rb").read(), "text/html; charset=utf-8")
            else:
                self.send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/refresh":
                return self.send(404, b"not found", "text/plain")
            if not lock.acquire(blocking=False):
                return self.send(200, json.dumps({"error": "A refresh is already running"}).encode(), "application/json")
            try:
                body = json.dumps(refresh(), separators=(",", ":"), ensure_ascii=False).encode()
            except Exception as e:
                print(f"refresh failed: {e}", file=sys.stderr)
                body = json.dumps({"error": str(e)}).encode()
            finally:
                lock.release()
            self.send(200, body, "application/json")

        def log_message(self, *args):
            pass

    url = f"http://localhost:{a.port}/"
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"Goal Ledger is at {url}  (Ctrl+C to stop)", flush=True)
    if a.every:
        threading.Thread(target=keep_fresh, daemon=True).start()
    if not a.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


HOME_DIR = os.path.join(os.path.expanduser("~"), "GoalLedger")
PLIST = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", "com.goalledger.plist")
WIN_START = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "GoalLedger.bat")


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def install(a):
    """Copy the dashboard to ~/GoalLedger and have it start at login and refresh itself."""
    here = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(HOME_DIR, exist_ok=True)
    for name in (os.path.basename(__file__), "goal_ledger.html", "odds_key.txt"):
        src, dst = os.path.join(here, name), os.path.join(HOME_DIR, name)
        if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if key and not os.path.exists(os.path.join(HOME_DIR, "odds_key.txt")):   # a login item does not see your shell's variables
        with open(os.path.join(HOME_DIR, "odds_key.txt"), "w") as f:
            f.write(key)
    script = os.path.join(HOME_DIR, os.path.basename(__file__))
    if not os.path.exists(os.path.join(HOME_DIR, "goal_ledger.html")):
        sys.exit("goal_ledger.html needs to be in the same folder as this script. Put it there and run this again.")
    need = []
    for mod in ("pandas", "pyarrow", "certifi"):
        try:
            __import__(mod)
        except ImportError:
            need.append(mod)
    if need:
        print(f"Installing {' and '.join(need)} (one time)...")
        base = [sys.executable, "-m", "pip", "install", "--user", "--quiet"] + need
        if subprocess.call(base) != 0 and subprocess.call(base + ["--break-system-packages"]) != 0:
            sys.exit(f"Could not install {' '.join(need)}. Run:  python3 -m pip install {' '.join(need)}   then run this again.")
    url = f"http://localhost:{a.port}/"
    cmd = [sys.executable, script, "--serve", "--no-open", "--port", str(a.port)]
    if sys.platform == "darwin":
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        args = "".join(f"<string>{c}</string>" for c in cmd)
        log = os.path.join(HOME_DIR, "ledger.log")
        with open(PLIST, "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
                    f'<key>Label</key><string>com.goalledger</string><key>ProgramArguments</key><array>{args}</array>'
                    '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>'
                    f'<key>WorkingDirectory</key><string>{HOME_DIR}</string>'
                    f'<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>'
                    '</dict></plist>\n')
        subprocess.call(["launchctl", "unload", PLIST], stderr=subprocess.DEVNULL)
        subprocess.call(["launchctl", "load", "-w", PLIST])
    elif os.name == "nt":
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        pyw = pyw if os.path.exists(pyw) else sys.executable
        with open(WIN_START, "w") as f:
            f.write(f'@echo off\nstart "" "{pyw}" "{script}" --serve --no-open --port {a.port}\n')
        subprocess.Popen([pyw] + cmd[1:], cwd=HOME_DIR, creationflags=0x00000008)
    else:
        subprocess.Popen(cmd, cwd=HOME_DIR, start_new_session=True,
                         stdout=open(os.path.join(HOME_DIR, "ledger.log"), "a"), stderr=subprocess.STDOUT)
        print("Started for this session. Add this to your login items to keep it:\n  " + " ".join(cmd))
    for _ in range(40):
        if port_open(a.port):
            break
        time.sleep(0.5)
    else:
        sys.exit(f"Set up, but the dashboard did not start. See {os.path.join(HOME_DIR, 'ledger.log')}")
    print(f"\nDone. Goal Ledger is at {url}\nBookmark that address. It starts when you log in and refreshes itself every two hours.\n"
          f"The first refresh is running now (it downloads about 100 MB of season files once); reload the page in a few minutes.")
    if not os.path.exists(os.path.join(HOME_DIR, "odds_key.txt")):
        print(f"No Odds API key found. Put it in {os.path.join(HOME_DIR, 'odds_key.txt')} to get prices.")
    if not a.no_open:
        webbrowser.open(url)


def uninstall(a):
    if sys.platform == "darwin" and os.path.exists(PLIST):
        subprocess.call(["launchctl", "unload", PLIST], stderr=subprocess.DEVNULL)
        os.remove(PLIST)
    if os.name == "nt" and os.path.exists(WIN_START):
        os.remove(WIN_START)
    print(f"Removed the login item. Delete {HOME_DIR} to remove the files. If the page still opens, restart your computer.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, help="the year the latest season starts (default: the season in progress)")
    ap.add_argument("--seasons", type=int, default=3, help="how many seasons to keep")
    ap.add_argument("--out", default="data.json")
    ap.add_argument("--html", help="standalone dashboard file to refresh in place")
    ap.add_argument("--offline", action="store_true", help="skip injuries and prices")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--every", type=float, default=2, help="hours between automatic refreshes while serving (0 = off)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    if a.install:
        return install(a)
    if a.uninstall:
        return uninstall(a)
    if pd is None:
        sys.exit("pandas and pyarrow are missing. Run:  python3 build_data.py --install")
    if a.serve:
        return serve(a)
    try:
        out = make(a.seasons, a.season, a.html, live=not a.offline)
        with open(a.out, "w") as f:
            json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
        if a.html:
            write_html(a.html, out)
            print(f"refreshed {a.html}")
    except RuntimeError as e:
        sys.exit(str(e))
    print(f"{len(out['games'])} games, {len(out['players'])} players, {len(out['sched'])} scheduled -> {a.out} "
          f"({os.path.getsize(a.out) / 1024:.0f} KB), through {out['through']}")


if __name__ == "__main__":
    main()
