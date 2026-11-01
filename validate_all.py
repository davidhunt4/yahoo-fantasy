"""Validate play-by-play scoring against Yahoo for every team and week so far; prints mismatches."""
import sys

import nflreadpy as nfl
import pandas as pd

from fantasy import LEAGUE_KEY, api_get, merge, numbered
from scoring import points_allowed, points_allowed_score, score_plays
from validate_week import SEASON, TEAM_FIX, yahoo_roster

weeks = range(1, int(sys.argv[1]) + 1) if len(sys.argv) > 1 else range(1, 8)
teams = [merge(e["team"][0])["team_key"] for e in numbered(api_get(f"league/{LEAGUE_KEY}/teams")["league"][1]["teams"])]

ids_all = nfl.load_ff_playerids().to_pandas()
ids = ids_all[ids_all.yahoo_id.notna()].assign(yahoo_id=lambda d: d.yahoo_id.astype(int)).drop_duplicates("yahoo_id")
import re
POS = {"K": "PK"}


def norm(n):
    n = re.sub(r"[.'’]", "", n.lower())
    return re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", n.strip())


byname = ids_all[ids_all.gsis_id.notna()].assign(key=lambda d: d.name.map(norm) + "|" + d.position).drop_duplicates("key").set_index("key").gsis_id

pbp = nfl.load_pbp(SEASON).to_pandas()
events = score_plays(pbp).merge(pbp[["game_id", "week"]].drop_duplicates(), on="game_id")
pts = events.groupby(["week", "actor"]).pts.sum()
wk_of = pbp[["game_id", "week"]].drop_duplicates().set_index("game_id").week
for (gid, team), pa in points_allowed(pbp).items():
    k = (wk_of[gid], f"DEF:{team}")
    pts[k] = pts.get(k, 0) + points_allowed_score(pa)

rows = []
for w in weeks:
    for t in teams:
        df = yahoo_roster(t, w).assign(week=w)
        df = df.merge(ids[["yahoo_id", "gsis_id"]], on="yahoo_id", how="left")
        miss = df.gsis_id.isna() & (df.pos != "DEF")
        df.loc[miss, "gsis_id"] = (df.name.map(norm) + "|" + df.pos.map(lambda p: POS.get(p, p)))[miss].map(byname)
        df["actor"] = df.apply(lambda r: f"DEF:{r.nfl_team}" if r.pos == "DEF" else r.gsis_id, axis=1)
        df["pbp_pts"] = [round(pts.get((w, a), 0), 2) for a in df.actor]
        rows.append(df)
allr = pd.concat(rows)
allr["diff"] = (allr.pbp_pts - allr.yahoo_pts).round(2)
played = allr[(allr.yahoo_pts != 0) | (allr.pbp_pts != 0)]
bad = played[played["diff"].abs() > 0.011]
print(f"{len(played)} player-weeks with points, {len(bad)} mismatches")
print(bad[["week", "team_key", "name", "pos", "slot", "yahoo_pts", "pbp_pts", "diff"]].to_string())
allr.to_pickle("validation.pkl")
