#!/usr/bin/env python3
"""
espn_probe.py - Phase 0b: one-off test of ESPN's unofficial NBA JSON endpoints.

Replaces nba_probe.py (cdn.nba.com is blocked by Akamai for everyone we tested).
Stdlib only, ~8 light requests, honest User-Agent (no header spoofing).

It checks:
  1. today's scoreboard (site.api.espn.com)
  2. the most recent finished game (walks back up to 12 days)
  3. a season-range scoreboard (can we list the whole schedule in one call?)
  4. the box score ("summary") on both known ESPN hosts
  5. that the box score has every stat a 9-category league needs
  6. that player points add up to the final score (sanity check)
  7. whether the server sends CORS headers (could browsers read it directly?)

Run it in GitHub Actions (workflow "ESPN data probe") AND on your PC
(`python espn_probe.py`) and compare the two reports.
NOTE: ESPN's endpoints are undocumented; their terms of use have NOT been read yet.
"""
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HOST_A = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
HOST_B = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba"
UA = "league-probe/0.2 (private fantasy league; one-off connectivity test)"
ORIGIN = "https://survive-coding.github.io"

# Box-score column labels we need (ESPN shows FG/3PT/FT as "made-attempted").
NEED = ["MIN", "PTS", "REB", "AST", "STL", "BLK", "TO", "FG", "3PT", "FT"]

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


def get(url, origin=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
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


def ymd(d):
    return d.strftime("%Y%m%d")


def completed(ev):
    return bool((((ev.get("status") or {}).get("type")) or {}).get("completed"))


def teams_of(ev):
    comp = (ev.get("competitions") or [{}])[0]
    out = {}
    for c in comp.get("competitors") or []:
        out[(c.get("team") or {}).get("abbreviation")] = c.get("score")
    return out


def box_players(summary):
    """Yield (team_abbr, {LABEL: value}, athlete_dict, did_not_play) for every listed player."""
    for team_block in (summary.get("boxscore") or {}).get("players") or []:
        abbr = (team_block.get("team") or {}).get("abbreviation")
        for grp in team_block.get("statistics") or []:
            labels = [str(x).upper() for x in (grp.get("labels") or grp.get("names") or [])]
            for a in grp.get("athletes") or []:
                vals = a.get("stats") or []
                yield abbr, dict(zip(labels, vals)), a.get("athlete") or {}, bool(a.get("didNotPlay"))


def main():
    where = "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else "local machine"
    now = datetime.now(timezone.utc)
    say(None, "Environment", f"{where}, Python {platform.python_version()}, {now:%Y-%m-%d %H:%M} UTC")

    # 1) Today's scoreboard
    r = get(f"{HOST_A}/scoreboard")
    j = as_json(r)
    sb_ok = r["status"] == 200 and isinstance(j, dict) and "events" in j
    say(sb_ok, "Scoreboard (today)", describe(r) + (f", {len(j['events'])} games" if sb_ok else ""))

    if sb_ok:
        acao = hget(get(f"{HOST_A}/scoreboard", origin=ORIGIN)["headers"], "access-control-allow-origin")
        say(None, "CORS header (Access-Control-Allow-Origin)",
            acao if acao else "not sent -> browsers on the site could not read it directly")

    # 2) Find the most recent day with a finished game (walk back up to 12 days)
    done_ev, done_day = None, None
    if sb_ok:
        for back in range(0, 13):
            day = now - timedelta(days=back)
            r = get(f"{HOST_A}/scoreboard?dates={ymd(day)}")
            j = as_json(r)
            evs = [e for e in (j or {}).get("events", []) if completed(e)] if isinstance(j, dict) else []
            if evs:
                done_ev, done_day = evs[-1], day
                say(True, f"Finished games on {ymd(day)}", f"{len(evs)} finished; using {done_ev.get('shortName')} (event {done_ev.get('id')})")
                break
        if not done_ev:
            say(False, "Finished game within last 12 days", "none found (offseason? wrong date range?)")

    # 3) Season-range scoreboard (one call for the whole schedule?)
    if sb_ok:
        start = datetime(now.year if now.month >= 10 else now.year - 1, 10, 1, tzinfo=timezone.utc)
        r = get(f"{HOST_A}/scoreboard?dates={ymd(start)}-{ymd(now)}&limit=1000")
        j = as_json(r)
        evs = (j or {}).get("events", []) if isinstance(j, dict) else []
        say(None, f"Range scoreboard {ymd(start)}-{ymd(now)} (one call)",
            describe(r) + f", {len(evs)} events, {sum(completed(e) for e in evs)} finished"
            + " -> if far fewer than expected, list the schedule day by day instead")

    # 4) Box score on both hosts
    summary = None
    if done_ev:
        eid = done_ev.get("id")
        for host in (HOST_A, HOST_B):
            r = get(f"{host}/summary?event={eid}")
            j = as_json(r)
            ok = r["status"] == 200 and isinstance(j, dict) and bool((j.get("boxscore") or {}).get("players"))
            say(ok, f"Box score (summary) via {host.split('/')[2]}", describe(r))
            if ok and summary is None:
                summary = j

    # 5) Stat completeness
    box_ok = False
    if summary:
        rows = list(box_players(summary))
        played = [x for x in rows if not x[3] and x[1].get("MIN") not in (None, "", "--", "0")]
        labels_seen = sorted({k for _, st, _, _ in rows for k in st})
        missing = [k for k in NEED if k not in labels_seen]
        box_ok = bool(played) and not missing
        say(box_ok, "Box score has every stat needed for 9 categories",
            f"{len(rows)} players listed, {len(played)} with minutes"
            + (f"; MISSING: {', '.join(missing)}" if missing else "") + f"; columns seen: {', '.join(labels_seen)}")

        # 6) Points cross-check against the final score
        score = teams_of(done_ev)
        sums = {}
        for abbr, st, _, dnp in rows:
            if not dnp:
                try:
                    sums[abbr] = sums.get(abbr, 0) + int(st.get("PTS") or 0)
                except ValueError:
                    pass
        mism = {t: (sums.get(t), score.get(t)) for t in score if str(sums.get(t)) != str(score.get(t))}
        say(not mism and bool(sums), "Player points add up to the final score",
            f"{sums} vs {score}" + (f"; MISMATCH {mism}" if mism else ""))

        if played:
            def pts(x):
                try:
                    return int(x[1].get("PTS") or 0)
                except ValueError:
                    return 0
            abbr, st, ath, _ = max(played, key=pts)
            say(None, "Sample line", f"{ath.get('displayName')} (ESPN id {ath.get('id')}, {abbr}): "
                + ", ".join(f"{k} {st.get(k)}" for k in NEED))

    # Verdict
    print()
    verdict = sb_ok and box_ok
    say(verdict, "OVERALL", "ESPN is readable and complete from here" if verdict else "ESPN is NOT fully usable from here")

    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write("## ESPN data probe\n\n")
            for ok, line in LINES:
                f.write(f"- {line}\n")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
