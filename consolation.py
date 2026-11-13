#!/usr/bin/env python3
"""
consolation.py - Phase 2: score the consolation bracket from the cached ESPN games.

    python consolation.py                       # reads data/consolation_input.json
    python consolation.py --today 2026-11-25    # pretend it is another day (testing)

Rules (as agreed with the commissioner, Nov 10 2026):
  * 9-category head-to-head: FG%, FT%, 3PT, PTS, REB, AST, STL, BLK, TO (lower TO wins).
    Each category is compared with the opponent's; the score is categories won (5-3-1 ...).
  * FG% and FT% come from summed makes and attempts, never averaged. A team with zero
    attempts has no percentage: the other team wins the category (both zero = tie).
  * Each team has a fixed 10-player lineup per round (positions are labels only, not checked).
    Every game a lineup player plays from Monday to Sunday of the round counts.
    Game days are US Eastern (the "day" field written by espn_fetch.py).
  * Tied on categories won -> the higher regular-season seed advances.
  * 6 teams: round 1 = seed 3 v 6 and 4 v 5 (seeds 1-2 have a bye). Round 2 reseeds:
    seed 1 plays the lowest remaining seed, seed 2 the other. Round 3 = final.

A round is "final" only when its Sunday has passed and all 7 days are cached with every
game finished; before that, totals are shown as a provisional leader and nobody advances.

Input  data/consolation_input.json   (see consolation_input.example.json)
Output data/consolation.json         (one file the site can read)
Stdlib only. Exit code 0 normally (warnings are listed in the output and printed),
2 if the input file is invalid.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction

CATS = [("FG%", "fg"), ("FT%", "ft"), ("3PT", "tpm"), ("PTS", "pts"), ("REB", "reb"),
        ("AST", "ast"), ("STL", "stl"), ("BLK", "blk"), ("TO", "to")]
SUMS = ["fgm", "fga", "ftm", "fta", "tpm", "pts", "reb", "ast", "stl", "blk", "to"]
LINEUP_SIZE = 10
SLOTS_DEFAULT = ["PG", "SG", "G", "SF", "PF", "F", "C", "C", "Util", "Util"]


class InputError(Exception):
    pass


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_date(s):
    return datetime.strptime(str(s).replace("-", ""), "%Y%m%d").date()


def load_games(games_dir, warnings):
    games = []
    if not os.path.isdir(games_dir):
        return games
    for name in sorted(os.listdir(games_dir)):
        if not name.endswith(".json"):
            continue
        try:
            g = read_json(os.path.join(games_dir, name))
        except (OSError, ValueError):
            warnings.append(f"unreadable game file {name}")
            continue
        if not g.get("day"):
            warnings.append(f"game {g.get('id')} has no 'day' (run espn_fetch.py again)")
            continue
        if not g.get("checks", {}).get("ok", False):
            warnings.append(f"game {g.get('id')} ({g.get('name')}) failed its checks but is included")
        games.append(g)
    return games


def day_status(days_dir, d):
    """'ok' (cached, all finished), 'open' (cached, games unfinished) or 'missing'."""
    try:
        data = read_json(os.path.join(days_dir, f"{d:%Y%m%d}.json"))
    except (OSError, ValueError):
        return "missing"
    events = data.get("events")
    if events is None:
        return "missing"
    return "ok" if all(e.get("completed") for e in events) else "open"


def team_totals(lineup, games, mon, sun):
    """Sum the lineup's stats over games whose day is within [mon, sun]."""
    lo, hi = f"{mon:%Y%m%d}", f"{sun:%Y%m%d}"
    ids = set(lineup)
    tot = {k: 0 for k in SUMS}
    gp = {i: 0 for i in lineup}
    for g in games:
        if not (lo <= g["day"] <= hi):
            continue
        for p in g["players"]:
            if p["espn_id"] in ids:
                gp[p["espn_id"]] += 1
                tot["fgm"] += p["fgm"]; tot["fga"] += p["fga"]
                tot["ftm"] += p["ftm"]; tot["fta"] += p["fta"]
                tot["tpm"] += p["tpm"]
                for k in ("pts", "reb", "ast", "stl", "blk", "to"):
                    tot[k] += p[k]
    tot["games"] = sum(gp.values())
    return tot, gp


def player_lines(raw, slots, pnames, games, mon, sun):
    """One line per filled lineup slot: who, which slot, games played and weekly sums."""
    lo, hi = f"{mon:%Y%m%d}", f"{sun:%Y%m%d}"
    out = []
    for n, pid in enumerate(raw):
        if not pid:
            continue
        info = pnames.get(pid) or {}
        line = {"slot": slots[n] if n < len(slots) else "", "id": pid,
                "name": info.get("name") or pid, "team": info.get("team") or "",
                "gp": 0, **{k: 0 for k in SUMS}}
        for g in games:
            if lo <= g["day"] <= hi:
                for p in g["players"]:
                    if p["espn_id"] == pid:
                        line["gp"] += 1
                        for k in SUMS:
                            line[k] += p[k]
        out.append(line)
    return out


def pct(made, att):
    return Fraction(made, att) if att else None


def compare(a, b):
    """Return per-category results for team totals a and b."""
    out = {}
    for cat, key in CATS:
        if key in ("fg", "ft"):
            va, vb = pct(a[key + "m"], a[key + "a"]), pct(b[key + "m"], b[key + "a"])
            da = None if va is None else round(float(va), 4)
            db = None if vb is None else round(float(vb), 4)
            if va is None and vb is None:
                w = "tie"
            elif va is None:
                w = "b"
            elif vb is None:
                w = "a"
            else:
                w = "a" if va > vb else "b" if vb > va else "tie"
        else:
            va, vb = a[key], b[key]
            da, db = va, vb
            if key == "to":                      # lower turnovers win
                w = "a" if va < vb else "b" if vb < va else "tie"
            else:
                w = "a" if va > vb else "b" if vb > va else "tie"
        out[cat] = {"a": da, "b": db, "winner": w}
    return out


def validate_lineup(round_no, team, ids, seen, warnings):
    if len(ids) != LINEUP_SIZE:
        warnings.append(f"round {round_no}: team {team} lineup has {len(ids)} players, expected {LINEUP_SIZE}")
    if len(set(ids)) != len(ids):
        warnings.append(f"round {round_no}: team {team} lineup repeats a player")
    for i in ids:
        if i in seen and seen[i] != team:
            warnings.append(f"round {round_no}: player {i} is in the lineups of teams {seen[i]} and {team}")
        seen[i] = team


def build(cfg, games_dir, today):
    warnings = []
    seeds = [str(x) for x in cfg.get("seeds", [])]
    mondays = [parse_date(m) for m in cfg.get("round_mondays", [])]
    if len(seeds) != 6 or len(set(seeds)) != 6:
        raise InputError("'seeds' must list 6 distinct team ids, best seed first")
    if len(mondays) != 3:
        raise InputError("'round_mondays' must list 3 dates (rounds 1 to 3)")
    for m in mondays:
        if m.weekday() != 0:
            raise InputError(f"{m} is not a Monday")
    if any(b - a < timedelta(days=7) for a, b in zip(mondays, mondays[1:])):
        raise InputError("rounds must start at least 7 days apart")
    seed_of = {t: i + 1 for i, t in enumerate(seeds)}
    names = {str(k): v for k, v in (cfg.get("names") or {}).items()}
    raw = {str(r): {str(t): [str(i).strip() for i in ids] for t, ids in teams.items()}
           for r, teams in (cfg.get("lineups") or {}).items()}          # slot order, blanks kept
    lineups = {r: {t: [i for i in ids if i] for t, ids in teams.items()} for r, teams in raw.items()}
    slots = cfg.get("slots") or SLOTS_DEFAULT
    pnames = cfg.get("players") or {}
    overrides = cfg.get("overrides") or []
    games = load_games(os.path.join(games_dir, "games"), warnings)
    days_dir = os.path.join(games_dir, "days")

    rounds_out, winners_prev, champion = [], None, None
    for r, mon in enumerate(mondays, 1):
        sun = mon + timedelta(days=6)
        # who plays whom
        if r == 1:
            pairs = [(seeds[2], seeds[5]), (seeds[3], seeds[4])]
        elif winners_prev is None:
            pairs = None
        elif r == 2:
            rem = sorted([seeds[0], seeds[1]] + winners_prev, key=seed_of.get)
            pairs = [(rem[0], rem[3]), (rem[1], rem[2])]
        else:
            rem = sorted(winners_prev, key=seed_of.get)
            pairs = [(rem[0], rem[1])]

        days = [mon + timedelta(days=i) for i in range(7)]
        missing = [f"{d:%Y-%m-%d}" for d in days if d <= today and day_status(days_dir, d) == "missing"]
        open_days = [f"{d:%Y-%m-%d}" for d in days if d <= today and day_status(days_dir, d) == "open"]
        if today < mon:
            status = "not started"
        elif today > sun and not missing and not open_days:
            status = "final"
        else:
            status = "in progress"
        if status == "in progress" and today > sun:
            warnings.append(f"round {r} is over but days are missing or unfinished: {missing + open_days}")
        elif status == "in progress" and missing:
            warnings.append(f"round {r}: days not cached yet: {missing}")

        rnd = {"round": r, "monday": f"{mon:%Y-%m-%d}", "sunday": f"{sun:%Y-%m-%d}",
               "status": status, "matchups": []}
        if pairs is None:
            rnd["note"] = "waiting for the previous round"
            rounds_out.append(rnd)
            winners_prev = None
            continue

        seen, round_winners, all_decided = {}, [], True
        for a, b in pairs:
            m = {"teams": [a, b], "seeds": [seed_of[a], seed_of[b]]}
            la, lb = lineups.get(str(r), {}).get(a), lineups.get(str(r), {}).get(b)
            if la is None or lb is None:
                m["status"] = "awaiting lineups"
                rnd["matchups"].append(m)
                all_decided = False
                continue
            validate_lineup(r, a, la, seen, warnings)
            validate_lineup(r, b, lb, seen, warnings)
            ta, gpa = team_totals(la, games, mon, sun)
            tb, gpb = team_totals(lb, games, mon, sun)
            if status != "not started":
                for t, gp in ((a, gpa), (b, gpb)):
                    idle = [i for i, n in gp.items() if n == 0]
                    if idle and status == "final":
                        warnings.append(f"round {r}: team {t} has {len(idle)} lineup player(s) with no games: {idle}")
            cats = compare(ta, tb)
            wa = sum(c["winner"] == "a" for c in cats.values())
            wb = sum(c["winner"] == "b" for c in cats.values())
            ties = 9 - wa - wb
            m.update({"totals": {a: ta, b: tb}, "categories": cats,
                      "score": {a: wa, b: wb, "ties": ties},
                      "players": {t: player_lines(raw.get(str(r), {}).get(t, []), slots, pnames, games, mon, sun)
                                  for t in (a, b)}})
            leader = a if wa > wb else b if wb > wa else None
            tiebreak = False
            winner = None
            ov = next((o for o in overrides if int(o.get("round", 0)) == r
                       and set(map(str, o.get("teams", []))) == {a, b}), None)
            if ov:
                winner = str(ov["winner"])
                m["override"] = {"winner": winner, "note": ov.get("note", "")}
            elif status == "final":
                if leader:
                    winner = leader
                else:
                    winner, tiebreak = (a if seed_of[a] < seed_of[b] else b), True
            m["leader"] = leader
            m["winner"] = winner
            m["tiebreak_by_seed"] = tiebreak
            m["status"] = "final" if winner else ("not started" if status == "not started" else "provisional")
            if winner:
                round_winners.append(winner)
            else:
                all_decided = False
            rnd["matchups"].append(m)
        rounds_out.append(rnd)
        winners_prev = round_winners if all_decided and len(round_winners) == len(pairs) else None
        if r == 3 and winners_prev:
            champion = winners_prev[0]

    return {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "as_of": f"{today:%Y-%m-%d}", "seeds": seeds, "names": names,
            "rounds": rounds_out, "champion": champion, "warnings": warnings}


def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score the consolation bracket.")
    ap.add_argument("--input", default="data/consolation_input.json")
    ap.add_argument("--games", default="data/nba", help="folder written by espn_fetch.py")
    ap.add_argument("--out", default="data/consolation.json")
    ap.add_argument("--today", help="YYYY-MM-DD, for testing (default: today, UTC)")
    a = ap.parse_args(argv)
    try:
        cfg = read_json(a.input)
        today = parse_date(a.today) if a.today else datetime.now(timezone.utc).date()
        res = build(cfg, a.games, today)
    except (OSError, ValueError, KeyError, InputError) as e:
        print(f"Input problem: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    try:                                    # keep the file (and the repo history) quiet when nothing changed
        prev = read_json(a.out)
        same = {k: v for k, v in prev.items() if k != "generated"} == {k: v for k, v in res.items() if k != "generated"}
    except (OSError, ValueError):
        same = False
    if not same:
        write_json(a.out, res)
    names = res["names"]
    nm = lambda t: names.get(t, t)
    for rnd in res["rounds"]:
        print(f"Round {rnd['round']} ({rnd['monday']} to {rnd['sunday']}): {rnd['status']}")
        for m in rnd["matchups"]:
            a_, b_ = m["teams"]
            if "score" in m:
                s = m["score"]
                tag = f"{s[a_]}-{s[b_]}-{s['ties']}"
                win = f" -> {nm(m['winner'])}" + (" (seed tiebreak)" if m["tiebreak_by_seed"] else "") if m["winner"] else \
                      (f" (leader: {nm(m['leader'])})" if m["leader"] else " (level)")
                print(f"  {nm(a_)} vs {nm(b_)}: {tag}{win}")
            else:
                print(f"  {nm(a_)} vs {nm(b_)}: {m['status']}")
    if res["champion"]:
        print(f"Champion: {nm(res['champion'])}")
    for w in res["warnings"]:
        print(f"[WARN] {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
