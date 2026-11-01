"""Compare play-by-play-derived fantasy points with Yahoo's player totals for one matchup."""
import sys

import nflreadpy as nfl
import pandas as pd

from fantasy import LEAGUE_KEY, api_get, merge, numbered
from scoring import points_allowed_score, score_plays

SEASON = 2026
TEAM_FIX = {"WSH": "WAS", "JAC": "JAX", "LAR": "LA"}


def yahoo_roster(team_key, week):
    d = api_get(f"team/{team_key}/roster;week={week}/players/stats;type=week;week={week}")
    out = []
    for e in numbered(d["team"][1]["roster"]["0"]["players"]):
        p = e["player"]
        info = merge(p[0])
        rest = merge(p[1:])
        out.append({
            "team_key": team_key,
            "yahoo_id": int(info["player_id"]),
            "name": info["name"]["full"],
            "nfl_team": TEAM_FIX.get(info["editorial_team_abbr"].upper(), info["editorial_team_abbr"].upper()),
            "pos": info["display_position"],
            "slot": merge(rest["selected_position"])["position"],
            "yahoo_pts": float(rest["player_points"]["total"]),
        })
    return pd.DataFrame(out)


if __name__ == "__main__":
    WEEK = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    TEAM_KEYS = sys.argv[2:] or [f"{LEAGUE_KEY}.t.9"]
    roster = pd.concat([yahoo_roster(k, WEEK) for k in TEAM_KEYS], ignore_index=True)

    ids = nfl.load_ff_playerids().to_pandas()
    ids_all = ids
    ids = ids[ids.yahoo_id.notna()].assign(yahoo_id=lambda d: d.yahoo_id.astype(int))
    roster = roster.merge(ids[["yahoo_id", "gsis_id"]].drop_duplicates("yahoo_id"), on="yahoo_id", how="left")
    # Rookies often lack a yahoo_id in the crosswalk; fall back to matching on name + team.
    byname = ids_all.assign(key=lambda d: d.name.str.lower() + "|" + d.team.map(lambda t: TEAM_FIX.get(t, t))).drop_duplicates("key").set_index("key").gsis_id
    miss = roster.gsis_id.isna() & (roster.pos != "DEF")
    roster.loc[miss, "gsis_id"] = (roster.name.str.lower() + "|" + roster.nfl_team)[miss].map(byname)
    roster["actor"] = roster.apply(lambda r: f"DEF:{r.nfl_team}" if r.pos == "DEF" else r.gsis_id, axis=1)

    pbp = nfl.load_pbp(SEASON).to_pandas()
    pbp = pbp[pbp.week == WEEK]
    events = score_plays(pbp)
    mine = events.groupby("actor").pts.sum()

    # Points allowed (final): opponent's final score. Variants are shown so we can see which Yahoo uses.
    final = pbp.sort_values("play_id").groupby("game_id").tail(1)
    for _, g in final.iterrows():
        for side, other in (("home", "away"), ("away", "home")):
            actor = f"DEF:{g[side + '_team']}"
            allowed = g[f"total_{other}_score"]
            mine[actor] = mine.get(actor, 0) + points_allowed_score(allowed)

    roster["pbp_pts"] = roster.actor.map(mine).fillna(0).round(2)
    roster["diff"] = (roster.pbp_pts - roster.yahoo_pts).round(2)
    print(roster[["team_key", "name", "pos", "slot", "yahoo_pts", "pbp_pts", "diff"]].to_string())
    for k in TEAM_KEYS:
        s = roster[(roster.team_key == k) & ~roster.slot.isin(["BN", "IR"])]
        print(k, "starters  yahoo", round(s.yahoo_pts.sum(), 2), " pbp", round(s.pbp_pts.sum(), 2))
