"""Yahoo roster fetching and Yahoo player -> nflverse player (gsis_id) mapping."""
import re

import pandas as pd

from nfldata import load

from fantasy import api_get, merge, numbered

TEAM_FIX = {"WSH": "WAS", "JAC": "JAX", "LAR": "LA"}
POS_FIX = {"K": "PK"}


def norm(name):
    name = re.sub(r"[.'’]", "", name.lower())
    return re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", name.strip())


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


def attach_actors(roster):
    """Add gsis_id and actor columns. Actor is the gsis_id for players and 'DEF:<TEAM>' for defenses."""
    allids = load("load_ff_playerids")
    by_yahoo = allids[allids.yahoo_id.notna()].assign(yahoo_id=lambda d: d.yahoo_id.astype(int)).drop_duplicates("yahoo_id")
    # Rookies often lack a yahoo_id (and the crosswalk's team codes differ), so fall back to name + position.
    by_name = (allids[allids.gsis_id.notna()]
               .assign(key=lambda d: d.name.map(norm) + "|" + d.position)
               .drop_duplicates("key").set_index("key").gsis_id)
    df = roster.merge(by_yahoo[["yahoo_id", "gsis_id"]], on="yahoo_id", how="left")
    miss = df.gsis_id.isna() & (df.pos != "DEF")
    df.loc[miss, "gsis_id"] = (df.name.map(norm) + "|" + df.pos.map(lambda p: POS_FIX.get(p, p)))[miss].map(by_name)
    df["actor"] = df.apply(lambda r: f"DEF:{r.nfl_team}" if r.pos == "DEF" else r.gsis_id, axis=1)
    return df
