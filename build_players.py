#!/usr/bin/env python3
"""
build_players.py - build data/nba/players.json, the player index used by the admin tool to
search and pick lineup players. Reads the cached games written by espn_fetch.py.

    python build_players.py                 # data/nba -> data/nba/players.json

Each entry: {"id": ESPN player id, "name", "team" (team of the most recent game),
             "last": last game day (YYYYMMDD), "gp": games played in the cache}.
Sorted by name. Only players who have played in a cached game appear, so the index grows as
the season is fetched. Stdlib only; the file is rewritten only when its content changes.
"""
import argparse
import json
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", default="data/nba")
    a = ap.parse_args()
    folder = os.path.join(a.games, "games")
    players = {}
    n_games = 0
    for f in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not f.endswith(".json"):
            continue
        with open(os.path.join(folder, f), encoding="utf-8") as fh:
            g = json.load(fh)
        n_games += 1
        day = g.get("day") or ""
        for p in g.get("players", []):
            e = players.setdefault(p["espn_id"], {"id": p["espn_id"], "name": p["name"],
                                                  "team": p["team"], "last": day, "gp": 0})
            e["gp"] += 1
            if day >= e["last"]:
                e.update(name=p["name"], team=p["team"], last=day)
    out = sorted(players.values(), key=lambda x: (x["name"] or "").lower())
    path = os.path.join(a.games, "players.json")
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    try:
        same = open(path, encoding="utf-8").read() == text
    except OSError:
        same = False
    if not same:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(f"{len(out)} players from {n_games} games -> {path}" + (" (unchanged)" if same else ""))


if __name__ == "__main__":
    main()
