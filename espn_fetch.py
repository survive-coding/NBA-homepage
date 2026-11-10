#!/usr/bin/env python3
"""
espn_fetch.py - Phase 1: cache finished NBA box scores from ESPN's unofficial API.

For each day in a date window it lists the games (scoreboard), and for every
FINISHED game not yet cached it downloads the box score and stores one compact
JSON file with each player's stats (the 10 numbers a 9-category league needs).

    python espn_fetch.py --from 2026-11-03 --to 2026-11-09
    python espn_fetch.py                      # yesterday and today
    python espn_fetch.py --from 2026-11-03 --to 2026-11-09 --refresh

Layout (default --out data/nba):
    data/nba/days/YYYYMMDD.json   the day's game list (so settled days are never re-requested)
    data/nba/games/<espnId>.json  one finished game: teams, scores, player lines, checks

Dates are the "game day" ESPN uses (US Eastern), which matches how fantasy days work.
Each game file carries "day" (YYYYMMDD, that game day); "date" is the UTC tip-off time and
can fall on the next calendar day for evening games, so scoring should use "day".
Stdlib only. Politeness: honest User-Agent, ~0.7 s between requests, no retries on 4xx.
NOTE: ESPN's endpoints are undocumented and their terms of use have NOT been read yet.
Every game is checked (stat columns present, player points == final score); problems are
stored in the game file under "checks" and printed, and make the exit code non-zero.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HOSTS = [
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba",
    "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba",
]
UA = "league-fetch/0.1 (private fantasy league; cached, polite)"
PAUSE = 0.7
NEED = ["MIN", "PTS", "REB", "AST", "STL", "BLK", "TO", "FG", "3PT", "FT"]


class FetchError(Exception):
    pass


def http_json(url, retries=2):
    """GET url and parse JSON. Retries network/5xx errors, never 4xx (they won't change)."""
    last = "unknown error"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read())
            time.sleep(PAUSE)
            return data
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if 400 <= e.code < 500:
                break
        except Exception as e:  # network, DNS, TLS, timeout, bad JSON
            last = f"{type(e).__name__}: {str(e)[:100]}"
        time.sleep(2 * (attempt + 1))
    raise FetchError(f"{url} -> {last}")


def to_int(x):
    try:
        return int(str(x).strip())
    except (ValueError, TypeError):
        return 0


def minutes(x):
    """'21' -> 21; '21:34' -> 21; '--' or '' -> 0."""
    return to_int(str(x).split(":")[0]) if x not in (None, "", "--") else 0


def made_att(x):
    """'6-10' -> (6, 10). Returns None if the text is not in that form."""
    try:
        a, b = str(x).split("-")
        return int(a), int(b)
    except (ValueError, AttributeError):
        return None


def compact_event(ev):
    comp = (ev.get("competitions") or [{}])[0]
    teams = {}
    for c in comp.get("competitors") or []:
        abbr = (c.get("team") or {}).get("abbreviation")
        teams[abbr] = {"home": c.get("homeAway") == "home", "score": to_int(c.get("score"))}
    return {
        "id": str(ev.get("id")),
        "date": ev.get("date"),
        "name": ev.get("shortName") or ev.get("name"),
        "season_type": (ev.get("season") or {}).get("type"),  # 1 pre, 2 regular, 3 post (None if absent)
        "completed": bool((((ev.get("status") or {}).get("type")) or {}).get("completed")),
        "teams": teams,
    }


def parse_box(summary, ev):
    """Turn an ESPN summary into our compact game record, with checks."""
    problems = []
    players = []
    sums = {}
    labels_seen = set()
    for block in (summary.get("boxscore") or {}).get("players") or []:
        abbr = (block.get("team") or {}).get("abbreviation")
        for grp in block.get("statistics") or []:
            labels = [str(x).upper() for x in (grp.get("labels") or grp.get("names") or [])]
            labels_seen.update(labels)
            for a in grp.get("athletes") or []:
                st = dict(zip(labels, a.get("stats") or []))
                if a.get("didNotPlay") or minutes(st.get("MIN")) <= 0:
                    continue
                fg, tp, ft = made_att(st.get("FG")), made_att(st.get("3PT")), made_att(st.get("FT"))
                ath = a.get("athlete") or {}
                if not (fg and tp and ft):
                    problems.append(f"unreadable shooting line for {ath.get('displayName')}: "
                                    f"FG={st.get('FG')} 3PT={st.get('3PT')} FT={st.get('FT')}")
                    fg, tp, ft = fg or (0, 0), tp or (0, 0), ft or (0, 0)
                row = {
                    "espn_id": str(ath.get("id")), "name": ath.get("displayName"), "team": abbr,
                    "min": minutes(st.get("MIN")), "pts": to_int(st.get("PTS")),
                    "reb": to_int(st.get("REB")), "ast": to_int(st.get("AST")),
                    "stl": to_int(st.get("STL")), "blk": to_int(st.get("BLK")), "to": to_int(st.get("TO")),
                    "fgm": fg[0], "fga": fg[1], "tpm": tp[0], "tpa": tp[1], "ftm": ft[0], "fta": ft[1],
                }
                players.append(row)
                sums[abbr] = sums.get(abbr, 0) + row["pts"]

    missing = [k for k in NEED if k not in labels_seen]
    if missing:
        problems.append("missing stat columns: " + ", ".join(missing))
    if not players:
        problems.append("no players with minutes")
    for abbr, t in ev["teams"].items():
        if sums.get(abbr) != t["score"]:
            problems.append(f"points mismatch for {abbr}: players {sums.get(abbr)} vs final {t['score']}")

    return {
        "id": ev["id"], "day": ev["day"], "date": ev["date"], "name": ev["name"],
        "season_type": ev["season_type"],
        "teams": ev["teams"], "players": players,
        "checks": {"ok": not problems, "problems": problems},
        "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def stamp(events, day):
    for e in events:
        e["day"] = f"{day:%Y%m%d}"
    return events


def get_day(day, out, today):
    """Return the compact events of one day, using the cached day file when it is settled."""
    path = os.path.join(out, "days", f"{day:%Y%m%d}.json")
    cached = read_json(path)
    if cached and cached.get("events") is not None:
        settled = all(e.get("completed") for e in cached["events"]) and (today - day).days >= 2
        if settled:
            return stamp(cached["events"], day), False
    last = None
    for host in HOSTS:
        try:
            data = http_json(f"{host}/scoreboard?dates={day:%Y%m%d}")
            events = [compact_event(e) for e in data.get("events") or []]
            write_json(path, {"day": f"{day:%Y%m%d}", "events": events,
                              "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            return stamp(events, day), True
        except FetchError as e:
            last = e
    raise last


def get_summary(event_id):
    last = None
    for host in HOSTS:
        try:
            return http_json(f"{host}/summary?event={event_id}")
        except FetchError as e:
            last = e
    raise last


def parse_day(s):
    s = s.replace("-", "")
    return datetime.strptime(s, "%Y%m%d").date()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Cache finished NBA box scores from ESPN.")
    ap.add_argument("--from", dest="start", help="first day, YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--to", dest="end", help="last day, YYYY-MM-DD (default: today)")
    ap.add_argument("--out", default="data/nba", help="output folder (default data/nba)")
    ap.add_argument("--refresh", action="store_true", help="re-download games already cached")
    ap.add_argument("--include-preseason", action="store_true", help="also keep preseason games")
    a = ap.parse_args(argv)

    today = datetime.now(timezone.utc).date()
    end = parse_day(a.end) if a.end else today
    start = parse_day(a.start) if a.start else end - timedelta(days=1)
    if start > end:
        print("--from is after --to", file=sys.stderr)
        return 2

    n_new = n_cached = n_skipped = 0
    failures, warnings = [], []
    day = start
    while day <= end:
        try:
            events, fetched = get_day(day, a.out, today)
        except FetchError as e:
            failures.append(f"{day:%Y-%m-%d} schedule: {e}")
            print(f"[FAIL] {day:%Y-%m-%d} schedule: {e}", flush=True)
            day += timedelta(days=1)
            continue
        done = [e for e in events if e["completed"]]
        print(f"[DAY ] {day:%Y-%m-%d}: {len(events)} games, {len(done)} finished"
              + ("" if fetched else " (settled, from cache)"), flush=True)
        for ev in done:
            if ev["season_type"] == 1 and not a.include_preseason:
                n_skipped += 1
                continue
            gpath = os.path.join(a.out, "games", f"{ev['id']}.json")
            old = read_json(gpath)
            if old and old.get("checks", {}).get("ok") and not a.refresh:
                if old.get("day") != ev["day"]:  # files written before "day" existed: patch, no download
                    old["day"] = ev["day"]
                    write_json(gpath, old)
                n_cached += 1  # good games are never re-downloaded; flagged ones are retried
                continue
            try:
                game = parse_box(get_summary(ev["id"]), ev)
            except FetchError as e:
                failures.append(f"{ev['name']} ({ev['id']}): {e}")
                print(f"[FAIL] {ev['name']} ({ev['id']}): {e}", flush=True)
                continue
            write_json(gpath, game)
            n_new += 1
            if game["checks"]["ok"]:
                print(f"[ OK ] {ev['name']} ({ev['id']}): {len(game['players'])} players", flush=True)
            else:
                warnings.append(f"{ev['name']} ({ev['id']}): " + "; ".join(game["checks"]["problems"]))
                print(f"[WARN] {ev['name']} ({ev['id']}): " + "; ".join(game["checks"]["problems"]), flush=True)
        day += timedelta(days=1)

    print(f"\nDone: {n_new} new, {n_cached} already cached, {n_skipped} preseason skipped, "
          f"{len(warnings)} with check problems, {len(failures)} failed.")
    return 1 if (failures or warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
