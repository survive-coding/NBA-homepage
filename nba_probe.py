#!/usr/bin/env python3
"""
nba_probe.py - Phase 0: one-off connectivity test for cdn.nba.com JSON files.

Makes ~7 light requests (stdlib only) and reports whether we can read:
  1. the schedule (staticData)
  2. a finished game's box score (liveData)
  3. the live scoreboard (liveData)
It also checks whether the box score has every stat a 9-category league needs,
whether a browser-like User-Agent behaves differently (diagnosis only), and
whether the server sends CORS headers (to know if browsers could read it directly).

Run it in GitHub Actions (workflow "NBA data probe") AND on your own PC
(`python nba_probe.py`) and compare the two reports.
"""
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "https://cdn.nba.com/static/json"
SCHEDULES = [f"{BASE}/staticData/scheduleLeagueV2_1.json", f"{BASE}/staticData/scheduleLeagueV2.json"]
SCOREBOARD = f"{BASE}/liveData/scoreboard/todaysScoreboard_00.json"
BOX = BASE + "/liveData/boxscore/boxscore_{}.json"
UA_HONEST = "league-probe/0.1 (private fantasy league; one-off connectivity test)"
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ORIGIN = "https://survive-coding.github.io"
NEED = ["points", "reboundsTotal", "assists", "steals", "blocks", "turnovers", "threePointersMade",
        "fieldGoalsMade", "fieldGoalsAttempted", "freeThrowsMade", "freeThrowsAttempted", "minutes"]

LINES = []


def say(ok, label, detail=""):
    mark = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
    line = f"[{mark}] {label}" + (f" - {detail}" if detail else "")
    LINES.append((ok, line))
    print(line, flush=True)


def hget(headers, name):
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v
    return None


def get(url, ua=UA_HONEST, origin=None):
    h = {"User-Agent": ua, "Accept": "application/json"}
    if origin:
        h["Origin"] = origin
    t = time.time()
    out = {"status": None, "headers": {}, "body": b"", "ms": 0, "err": None}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=25) as r:
            out.update(status=r.status, headers=dict(r.headers), body=r.read())
    except urllib.error.HTTPError as e:
        out.update(status=e.code, headers=dict(e.headers or {}), body=e.read()[:400], err=f"HTTP {e.code}")
    except Exception as e:  # network, DNS, TLS, timeout
        out["err"] = f"{type(e).__name__}: {str(e)[:120]}"
    out["ms"] = int((time.time() - t) * 1000)
    time.sleep(0.7)  # be polite
    return out


def as_json(resp):
    try:
        return json.loads(resp["body"])
    except Exception:
        return None


def describe(resp):
    if resp["status"] is None:
        return resp["err"]
    ctype = hget(resp["headers"], "content-type") or "?"
    html = "html" in ctype.lower() or resp["body"][:1] == b"<"
    return f"HTTP {resp['status']}, {len(resp['body'])/1024:.0f} KB, {resp['ms']} ms, {ctype.split(';')[0]}" + (
        " (HTML page, probably a block page)" if html else "")


def latest_finished(sched):
    games = []
    for d in (sched.get("leagueSchedule") or {}).get("gameDates") or []:
        for g in d.get("games") or []:
            games.append(g)
    done = [g for g in games if g.get("gameStatus") == 3]
    done.sort(key=lambda g: g.get("gameDateTimeUTC") or "")
    return games, done


def main():
    where = "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else "local machine"
    say(None, "Environment", f"{where}, Python {platform.python_version()}, {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")

    # 1) Schedule
    sched, sched_url = None, None
    for url in SCHEDULES:
        r = get(url)
        j = as_json(r)
        ok = r["status"] == 200 and isinstance(j, dict)
        say(ok, f"Schedule ({url.rsplit('/', 1)[1]})", describe(r))
        if ok and sched is None:
            sched, sched_url = j, url
    if sched_url:
        rb = get(sched_url, ua=UA_BROWSER)
        say(None, "Same file with a browser-like User-Agent (diagnosis only)", describe(rb))
        rc = get(sched_url, origin=ORIGIN)
        acao = hget(rc["headers"], "access-control-allow-origin")
        say(None, "CORS header (Access-Control-Allow-Origin)", acao if acao else "not sent -> browsers on the site could not read it directly")

    # 2) Pick a finished game, read its box score
    box_ok = False
    if sched:
        league = sched.get("leagueSchedule") or {}
        games, done = latest_finished(sched)
        say(bool(done), "Schedule content",
            f"season {league.get('seasonYear')}, {len(games)} games, {len(done)} finished"
            + (f", latest finished: {done[-1].get('gameDateTimeUTC')}" if done else ""))
        if done:
            gid = done[-1].get("gameId")
            r = get(BOX.format(gid))
            j = as_json(r)
            game = (j or {}).get("game") if isinstance(j, dict) else None
            box_ok = r["status"] == 200 and isinstance(game, dict)
            say(box_ok, f"Box score for game {gid}", describe(r))
            if box_ok:
                players = []
                for side in ("homeTeam", "awayTeam"):
                    players += (game.get(side) or {}).get("players") or []
                played = [p for p in players if (p.get("statistics") or {}).get("minutes")]
                missing = sorted({k for p in played for k in NEED if k not in (p.get("statistics") or {})})
                say(not missing and bool(played), "Box score has every stat needed for 9 categories",
                    f"{len(players)} players listed, {len(played)} with minutes" + (f"; MISSING: {', '.join(missing)}" if missing else ""))
                if played:
                    top = max(played, key=lambda p: (p["statistics"].get("points") or 0))
                    s = top["statistics"]
                    say(None, "Sample line", f"{top.get('name')} (id {top.get('personId')}): {s.get('points')} pts, "
                        f"{s.get('reboundsTotal')} reb, {s.get('assists')} ast, {s.get('steals')} stl, {s.get('blocks')} blk, "
                        f"{s.get('turnovers')} to, {s.get('threePointersMade')} 3pm, FG {s.get('fieldGoalsMade')}/{s.get('fieldGoalsAttempted')}, "
                        f"FT {s.get('freeThrowsMade')}/{s.get('freeThrowsAttempted')}, min {s.get('minutes')}")

    # 3) Live scoreboard
    r = get(SCOREBOARD)
    j = as_json(r)
    sb = ((j or {}).get("scoreboard") or {}) if isinstance(j, dict) else {}
    say(r["status"] == 200 and bool(sb), "Live scoreboard", describe(r) + (f", {len(sb.get('games') or [])} games today" if sb else ""))

    # Verdict
    sched_pass = sched is not None
    verdict = sched_pass and box_ok
    print()
    say(verdict, "OVERALL", "cdn.nba.com is readable from here" if verdict else "cdn.nba.com is NOT fully readable from here")

    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write("## NBA data probe\n\n")
            for ok, line in LINES:
                f.write(f"- {line}\n")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
