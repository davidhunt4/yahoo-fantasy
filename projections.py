"""Sleeper weekly projections converted to this league's points.

These are third-party projections, not Yahoo's, so only large deviations from them are meaningful.
Run `python projections.py WEEK` before a week's first kickoff to save a snapshot; a snapshot taken
before kickoff is used in preference to a live fetch (which may reflect in-game adjustments).
"""
import datetime as dt
import glob
import json
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from nfldata import load
from players import norm
from scoring import points_allowed_score

SEASON = 2026
ET = ZoneInfo("America/New_York")
SNAP_DIR = os.path.join(os.path.dirname(__file__), "projections")
POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]
PA_TIERS = {"pts_allow_0": 10, "pts_allow_1_6": 7, "pts_allow_7_13": 4, "pts_allow_14_20": 1,
            "pts_allow_21_27": 0, "pts_allow_28_34": -1, "pts_allow_35p": -4}


def fetch_sleeper(week, tries=4):
    qs = "&".join(f"position[]={p}" for p in POSITIONS)
    url = f"https://api.sleeper.app/projections/nfl/{SEASON}/{week}?season_type=regular&{qs}&order_by=pts_ppr"
    for i in range(tries):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            return [x for x in r.json() if x.get("stats")]
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def save_snapshot(week):
    os.makedirs(SNAP_DIR, exist_ok=True)
    now = dt.datetime.now(ET)
    path = os.path.join(SNAP_DIR, f"sleeper_wk{week}_{now:%Y%m%d_%H%M%S}.json")
    with open(path, "w") as f:
        json.dump({"fetched_at": now.isoformat(), "week": week, "rows": fetch_sleeper(week)}, f)
    return path


def get_rows(week, first_kick=None):
    """Prefer a snapshot taken before the week's first kickoff; else the newest snapshot; else fetch live."""
    snaps = []
    for p in sorted(glob.glob(os.path.join(SNAP_DIR, f"sleeper_wk{week}_*.json"))):
        d = json.load(open(p))
        snaps.append((dt.datetime.fromisoformat(d["fetched_at"]), d["rows"], p))
    pre = [s for s in snaps if first_kick and s[0] < first_kick]
    if pre:
        t, rows, p = pre[-1]
        return rows, {"source": "sleeper snapshot taken before kickoff", "fetched_at": t.isoformat()}
    if snaps:
        t, rows, p = snaps[-1]
        return rows, {"source": "sleeper snapshot taken after kickoff", "fetched_at": t.isoformat()}
    return fetch_sleeper(week), {"source": "sleeper live fetch (Sleeper's stored projection for the week; no pre-kickoff snapshot to confirm timing)",
                                 "fetched_at": dt.datetime.now(ET).isoformat()}


def league_points(s, pos):
    """Convert a Sleeper projected stat line into points under this league's scoring."""
    g = lambda k: s.get(k) or 0
    if pos == "K":
        return (3 * (g("fgm_0_19") + g("fgm_20_29") + g("fgm_30_39")) + 4 * g("fgm_40_49") + 5 * g("fgm_50p") + g("xpm"))
    if pos == "DEF":
        pa = next((v for k, v in PA_TIERS.items() if s.get(k)), None)
        if pa is None and s.get("pts_allow") is not None:
            pa = points_allowed_score(round(s["pts_allow"]))
        return (g("sack") + 2 * g("int") + 2 * g("fum_rec") + 2 * g("safe") + 2 * g("blk_kick")
                + 6 * g("def_td") + 6 * g("st_td") + (pa or 0))
    return (0.04 * g("pass_yd") + 4 * g("pass_td") - g("pass_int") + 0.1 * (g("rush_yd") + g("rec_yd"))
            + 6 * (g("rush_td") + g("rec_td")) + 2 * (g("pass_2pt") + g("rush_2pt") + g("rec_2pt")) - 2 * g("fum_lost"))


# Points beyond which a result is "far" from the (approximate) projection, by position.
FAR = {"QB": 12, "RB": 12, "WR": 12, "TE": 10, "K": 8, "DEF": 9}


def vs_projection(actual, proj, pos):
    """Coarse comparison only; third-party projections aren't precise enough to quote a point difference."""
    if proj is None:
        return None
    diff = actual - proj
    far = FAR.get(pos, 12)
    return "far_above" if diff >= far else "far_below" if diff <= -far else "in_line"


def starter_projections(starters, rows):
    """Projected league points per starter (by actor); None where Sleeper has no projection."""
    ids = load("load_ff_playerids")
    ids = ids[ids.sleeper_id.notna() & ids.gsis_id.notna()]
    sleeper2gsis = {str(int(float(s))): g for s, g in zip(ids.sleeper_id, ids.gsis_id)}
    by_gsis, by_name, by_team = {}, {}, {}
    for r in rows:
        p = r["player"]
        pos = p.get("position")
        pts = league_points(r["stats"], pos)
        if pos == "DEF":
            by_team[r["player_id"]] = pts
            continue
        g = sleeper2gsis.get(str(r["player_id"]))
        if g:
            by_gsis[g] = pts
        by_name[norm(f"{p.get('first_name', '')} {p.get('last_name', '')}") + "|" + pos] = pts
    out = {}
    for s in starters.itertuples():
        if s.pos == "DEF":
            out[s.actor] = by_team.get(s.nfl_team)
        else:
            out[s.actor] = by_gsis.get(s.gsis_id, by_name.get(norm(s.name) + "|" + s.pos))
    return out


if __name__ == "__main__":
    # No argument: snapshot Sleeper's current NFL week.
    wk = int(sys.argv[1]) if len(sys.argv) > 1 else requests.get("https://api.sleeper.app/v1/state/nfl", timeout=30).json()["week"]
    print(f"week {wk}: saved", save_snapshot(wk))
