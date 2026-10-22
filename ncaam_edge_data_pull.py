#!/usr/bin/env python3
"""
ncaam_edge_data_pull.py  (NCAAM Edge Model v1.0 companion)

Writes ncaam_model_data.json for the NCAAM Edge Model HTML:
  teams     BartTorvik ratings (adjoe, adjde, adjt, barthag, ...)
  schedule  ESPN scoreboard games (neutral-site flag, venue, tournament notes, scores)
  odds_raw  The Odds API FanDuel + DraftKings lines, unmodified (the HTML parses them)

Keys: keys.txt next to this script (ODDS_API_KEY=...) wins over environment variables.
Name matching is NOT done here – the HTML does it, visibly, so nothing is silently zeroed.

If BartTorvik cannot be reached, or the new season is not published yet, the pull:
  1) uses torvik_manual.csv (if you drop one next to the script), else
  2) falls back to the previous season and sets stale_prior_season=true, else
  3) keeps the teams from the last good JSON.
It never overwrites good data with an empty result.
"""
import csv, datetime as dt, io, json, os, sys, time

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "ncaam_model_data.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; EdgeReportPull/1.0)"}
ODDS_URL = ("https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds"
            "?apiKey={key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american"
            "&bookmakers=fanduel,draftkings")
ESPN_URL = ("https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/"
            "scoreboard?dates={d}&groups=50&limit=400")
TORVIK_CSV = "https://barttorvik.com/{year}_team_results.csv"


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- keys
def clean_key(v):
    v = (v or "").strip().strip("'\"").strip()
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    return v.lstrip("+").strip()


def load_keys():
    keys = {}
    for name in ("ODDS_API_KEY",):
        keys[name] = clean_key(os.environ.get(name, ""))
    p = os.path.join(HERE, "keys.txt")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() in keys and clean_key(v):
                    keys[k.strip()] = clean_key(v)
    for k, v in keys.items():
        log(f"  key {k}: " + (f"len {len(v)} ({v[:2]}…{v[-2:]})" if v else "MISSING"))
    return keys


# ---------------------------------------------------------------- season
def season_year(today=None):
    t = today or dt.date.today()
    return t.year + 1 if t.month >= 8 else t.year   # 2026-27 season -> 2027


# ---------------------------------------------------------------- torvik
def to_f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_torvik_csv(text):
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        return []
    h = [c.strip().lower().replace(" ", "").replace("_", "").replace(".", "") for c in rows[0]]

    def col(*names):
        for n in names:
            if n in h:
                return h.index(n)
        return -1
    c = dict(team=col("team"), conf=col("conf"), record=col("record"), adjoe=col("adjoe"), adjde=col("adjde"),
             barthag=col("barthag"), adjt=col("adjt", "adjtempo", "tempo"), sos=col("sos"), wab=col("wab"), rank=col("rank"))
    if min(c["team"], c["adjoe"], c["adjde"], c["adjt"]) < 0:
        log("  Torvik CSV header not recognised:", rows[0][:12])
        return []
    out = []
    for r in rows[1:]:
        if len(r) <= max(c.values()):
            continue
        t = dict(team=r[c["team"]].strip(), conf=r[c["conf"]] if c["conf"] >= 0 else "", record=r[c["record"]] if c["record"] >= 0 else "",
                 adjoe=to_f(r[c["adjoe"]]), adjde=to_f(r[c["adjde"]]), adjt=to_f(r[c["adjt"]]),
                 barthag=to_f(r[c["barthag"]]) if c["barthag"] >= 0 else None,
                 sos=to_f(r[c["sos"]]) if c["sos"] >= 0 else None, wab=to_f(r[c["wab"]]) if c["wab"] >= 0 else None,
                 rank=to_f(r[c["rank"]]) if c["rank"] >= 0 else None)
        if t["team"] and t["adjoe"] and t["adjde"] and t["adjt"]:
            out.append(t)
    return out


def sane(teams):
    if len(teams) < 300:
        return False
    bad = [t for t in teams if not (60 < t["adjoe"] < 150 and 60 < t["adjde"] < 150 and 50 < t["adjt"] < 90)]
    return len(bad) <= 5


def fetch_torvik(year):
    url = TORVIK_CSV.format(year=year)
    try:
        r = requests.get(url, headers=UA, timeout=30)
        log(f"  Torvik {year}: HTTP {r.status_code}, {len(r.text)} chars")
        if r.status_code == 200 and "adjoe" in r.text[:600].lower():
            teams = parse_torvik_csv(r.text)
            if sane(teams):
                return teams
            log(f"  Torvik {year}: parsed {len(teams)} teams but failed sanity checks")
        else:
            log("  Torvik response is not the CSV (blocked or not published yet)")
    except Exception as e:
        log(f"  Torvik {year} failed: {e}")
    return []


def get_teams(prev):
    yr = season_year()
    teams = fetch_torvik(yr)
    if teams:
        return teams, yr, False, ""
    man = os.path.join(HERE, "torvik_manual.csv")
    if os.path.exists(man):
        teams = parse_torvik_csv(open(man, encoding="utf-8").read())
        if sane(teams):
            return teams, yr, False, "ratings from torvik_manual.csv"
    teams = fetch_torvik(yr - 1)
    if teams:
        return teams, yr - 1, True, f"new season not published; using {yr - 1} ratings"
    if prev and prev.get("teams"):
        return prev["teams"], prev.get("season"), True, "Torvik unreachable; kept last good ratings"
    return [], yr, True, "no ratings available"


# ---------------------------------------------------------------- espn
def fetch_espn(days_back=1, days_fwd=7):
    out, seen = [], set()
    today = dt.date.today()
    for i in range(-days_back, days_fwd + 1):
        d = today + dt.timedelta(days=i)
        try:
            r = requests.get(ESPN_URL.format(d=d.strftime("%Y%m%d")), headers=UA, timeout=20)
            if r.status_code != 200:
                continue
            for ev in r.json().get("events", []):
                comp = (ev.get("competitions") or [{}])[0]
                cs = comp.get("competitors") or []
                home = next((c for c in cs if c.get("homeAway") == "home"), None)
                away = next((c for c in cs if c.get("homeAway") == "away"), None)
                if not home or not away or ev.get("id") in seen:
                    continue
                seen.add(ev.get("id"))
                t = (ev.get("status") or {}).get("type") or {}

                def sc(c):
                    try:
                        return float(c.get("score"))
                    except (TypeError, ValueError):
                        return None
                out.append(dict(id=ev.get("id"), date=ev.get("date"), away=away["team"]["displayName"], home=home["team"]["displayName"],
                                neutral=bool(comp.get("neutralSite")), venue=(comp.get("venue") or {}).get("fullName", ""),
                                notes=" | ".join(n.get("headline", "") for n in comp.get("notes", [])),
                                status=t.get("shortDetail", ""), completed=bool(t.get("completed")),
                                awayScore=sc(away), homeScore=sc(home)))
        except Exception as e:
            log(f"  ESPN {d}: {e}")
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------- odds
def fetch_odds(key):
    if not key:
        log("  ODDS_API_KEY missing – skipping lines")
        return []
    try:
        r = requests.get(ODDS_URL.format(key=key), timeout=30)
        log(f"  Odds API: HTTP {r.status_code} · used {r.headers.get('x-requests-used', '?')} · left {r.headers.get('x-requests-remaining', '?')}")
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log(f"  Odds API failed: {e}")
    return []


# ---------------------------------------------------------------- main
def main():
    log(f"NCAAM Edge Model pull v{VERSION} · {dt.datetime.utcnow():%Y-%m-%d %H:%M} UTC")
    keys = load_keys()
    prev = None
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            prev = None
    teams, season, stale, note = get_teams(prev)
    if not teams:
        log("No ratings obtained and nothing to fall back on – leaving the existing file untouched.")
        sys.exit(1)
    sched = fetch_espn()
    odds = fetch_odds(keys["ODDS_API_KEY"])
    if not odds and prev and prev.get("odds_raw"):
        log("  keeping previous odds_raw (fresh pull returned nothing)")
        odds = prev["odds_raw"]
    data = dict(version=VERSION, generated_at=dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                season=season, stale_prior_season=stale, note=note, teams=teams, schedule=sched, odds_raw=odds,
                _counts=dict(teams=len(teams), schedule=len(sched), odds=len(odds)))
    tmp = OUT + ".tmp"
    json.dump(data, open(tmp, "w", encoding="utf-8"), separators=(",", ":"))
    os.replace(tmp, OUT)
    log(f"Wrote {OUT}: {len(teams)} teams · {len(sched)} schedule games · {len(odds)} odds events" + (f" · NOTE: {note}" if note else ""))


if __name__ == "__main__":
    main()
