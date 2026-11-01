"""Matchup context beyond the scoring timeline: records/standings, league comparison, team projections,
starter availability (bye / inactive / injured in game), bench, and transactions."""
import datetime as dt
import re
from zoneinfo import ZoneInfo

import pandas as pd

from nfldata import load

from fantasy import LEAGUE_KEY, api_get, merge, numbered
from players import TEAM_FIX, yahoo_roster
from projections import get_rows, starter_projections, vs_projection

ET = ZoneInfo("America/New_York")
SEASON = 2026
BENCH = ("BN", "IR")
SLUGFEST_PCT, SLOG_PCT = 80, 20  # percentile of combined score among this season's matchups


# ---- results / standings -------------------------------------------------------------------------

def weekly_results(upto):
    """One row per team per week from the scoreboard (week 1..upto)."""
    rows = []
    for w in range(1, upto + 1):
        sb = api_get(f"league/{LEAGUE_KEY}/scoreboard;week={w}")["league"][1]["scoreboard"]["0"]["matchups"]
        for e in numbered(sb):
            m = e["matchup"]
            teams = []
            for t in numbered(m["0"]["teams"]):
                info, rest = merge(t["team"][0]), merge(t["team"][1:])
                teams.append({"team_key": info["team_key"], "name": info["name"],
                              "points": float(rest["team_points"]["total"]),
                              "projected": float(rest["team_projected_points"]["total"])})
            done = m["status"] == "postevent"
            tied = str(m.get("is_tied")) in ("1", "True", "true")
            for i, t in enumerate(teams):
                res = None if not done else "T" if tied else "W" if m.get("winner_team_key") == t["team_key"] else "L"
                rows.append({"week": w, **t, "opp_key": teams[1 - i]["team_key"], "opp_points": teams[1 - i]["points"],
                             "result": res, "status": m["status"], "week_start": m["week_start"], "week_end": m["week_end"]})
    return pd.DataFrame(rows)


def standings_through(res, week):
    """Standings after `week`: win% then points for (verified against Yahoo's own standings)."""
    r = res[(res.week <= week) & (res.status == "postevent")]
    g = r.groupby(["team_key", "name"]).agg(
        W=("result", lambda s: int((s == "W").sum())), L=("result", lambda s: int((s == "L").sum())),
        T=("result", lambda s: int((s == "T").sum())), PF=("points", "sum"), PA=("opp_points", "sum")).reset_index()
    g["pct"] = (g.W + 0.5 * g["T"]) / (g.W + g.L + g["T"])
    g = g.sort_values(["pct", "PF"], ascending=False).reset_index(drop=True)
    g["rank"] = g.index + 1
    g["PF"], g["PA"] = g.PF.round(2), g.PA.round(2)
    return g


def records_and_standings(res, week, team_keys, names):
    before, after = standings_through(res, week - 1), standings_through(res, week)
    out = {}
    for k in team_keys:
        b = before[before.team_key == k]
        a = after[after.team_key == k].iloc[0]
        rec = lambda r: f"{r.W}-{r.L}" + (f"-{r['T']}" if r["T"] else "")
        out[names[k]] = {
            "record_before": rec(b.iloc[0]) if len(b) else "0-0", "record_after": rec(a),
            "rank_before": int(b.iloc[0]["rank"]) if len(b) else None, "rank_after": int(a["rank"]),
            "points_for_after": float(a.PF), "points_against_after": float(a.PA),
        }
    table = [{"rank": int(r["rank"]), "team": r["name"], "record": f"{r.W}-{r.L}" + (f"-{r['T']}" if r["T"] else ""), "points_for": float(r.PF)}
             for _, r in after.iterrows()]
    return out, table


# ---- league comparison / projections -------------------------------------------------------------

def league_context(res, week, team_keys, names):
    cur = res[res.week == week].set_index("team_key")
    hist = res[(res.week < week) & (res.status == "postevent")]
    n = len(cur)
    ranks = cur.points.rank(ascending=False, method="min").astype(int)
    teams = {}
    for k in team_keys:
        prev = hist[hist.team_key == k].points
        teams[names[k]] = {
            "score": cur.points[k], "league_rank_this_week": f"{ranks[k]} of {n}",
            "season_avg_before": round(prev.mean(), 2) if len(prev) else None,
            "vs_season_avg": round(cur.points[k] - prev.mean(), 2) if len(prev) else None,
        }
    # Combined score of every matchup so far this season (one row per matchup).
    done = res[res.status == "postevent"]
    m = done[done.team_key < done.opp_key].assign(total=lambda d: d.points + d.opp_points)
    this = m[m.week == week]
    pair = [r for r in m.itertuples() if r.week == week and {r.team_key, r.opp_key} == set(team_keys)][0]
    pct = round(float((m.total < pair.total).mean() * 100 + (m.total == pair.total).mean() * 50), 1)
    label = "slugfest" if pct >= SLUGFEST_PCT else "slog" if pct <= SLOG_PCT else "typical"
    return {
        "combined_score": round(pair.total, 2), "margin": round(abs(pair.points - pair.opp_points), 2),
        "combined_percentile_this_season": pct, "label": label,
        "combined_rank_this_week": f"{int((this.total > pair.total).sum()) + 1} of {len(this)}",
        "combined_rank_this_season": f"{int((m.total > pair.total).sum()) + 1} of {len(m)}",
        "league_avg_team_score_this_week": round(cur.points.mean(), 2),
        "league_avg_team_score_season": round(done.points.mean(), 2),
        "highest_score_this_week": {"team": cur.name[cur.points.idxmax()], "score": cur.points.max()},
        "lowest_score_this_week": {"team": cur.name[cur.points.idxmin()], "score": cur.points.min()},
        "teams": teams,
    }


def projections(res, week, team_keys, names):
    cur = res[res.week == week].set_index("team_key")
    out = {names[k]: {"projected": cur.projected[k], "actual": cur.points[k], "vs_projection": round(cur.points[k] - cur.projected[k], 2)}
           for k in team_keys}
    a, b = team_keys
    pw = a if cur.projected[a] > cur.projected[b] else b
    aw = a if cur.points[a] > cur.points[b] else b
    out["projected_winner"], out["actual_winner"], out["upset"] = names[pw], names[aw], pw != aw
    return out


# ---- starter availability ------------------------------------------------------------------------

_INJURED = re.compile(r"\b([A-Z]{2,3})-(\d{1,2})-[A-Za-z.'’\-]+(?: (?:Jr\.|Sr\.|II|III|IV))? was injured during the play")


def _clean(v):
    return None if pd.isna(v) else v


def availability(week, starters, pbp):
    """Per starter: bye / inactive (not active for the game) / did not play / played, plus injury-report
    designation and whether they were hurt on a play during the game."""
    rw = load("load_rosters_weekly", SEASON)
    rw = rw[rw.week == week].drop_duplicates("gsis_id").set_index("gsis_id")
    inj = load("load_injuries", SEASON)
    inj = inj[inj.week == week].drop_duplicates("gsis_id").set_index("gsis_id")
    ids = load("load_ff_playerids")
    pfr2gsis = ids[ids.pfr_id.notna() & ids.gsis_id.notna()].drop_duplicates("pfr_id").set_index("pfr_id").gsis_id
    sn = load("load_snap_counts", SEASON)
    sn = sn[sn.week == week].assign(gsis_id=lambda d: d.pfr_player_id.map(pfr2gsis))
    snaps = sn.groupby("gsis_id")[["offense_snaps", "defense_snaps", "st_snaps"]].sum().sum(axis=1)
    playing = set(pbp.home_team) | set(pbp.away_team)

    # Who got hurt on a play (matched on team + jersey number).
    jersey = {(r.team, int(r.jersey_number)): g for g, r in rw.iterrows() if pd.notna(r.jersey_number)}
    hurt = {}
    for r in pbp[pbp.desc.str.contains("was injured during the play", na=False)].itertuples():
        for team, num in _INJURED.findall(r.desc):
            g = jersey.get((team, int(num)))
            if g and g not in hurt:
                hurt[g] = {"quarter": int(r.qtr), "clock": r.time, "play": re.sub(r"^\(\d+:\d+\)\s*(\([^)]*\)\s*)?", "", r.desc)[:160]}

    out = {}
    for s in starters.itertuples():
        d = {}
        if s.nfl_team not in playing:
            d["availability"] = "bye"
        elif s.pos == "DEF":
            d["availability"] = "played"
        else:
            status = rw.status.get(s.gsis_id)
            snap = float(snaps.get(s.gsis_id, 0))
            d["roster_status"] = status
            if status is not None and status != "ACT":
                d["availability"] = "inactive"  # not active for the game: a manager error, not bad luck
            elif snap == 0 and s.yahoo_pts == 0 and s.pos != "K":
                d["availability"] = "active_but_did_not_play"
            else:
                d["availability"] = "played"
        if s.gsis_id in inj.index:
            i = inj.loc[s.gsis_id]
            if pd.notna(i.report_status) or pd.notna(i.report_primary_injury):
                d["injury_report"] = {"status": _clean(i.report_status), "injury": _clean(i.report_primary_injury),
                                      "practice": _clean(i.practice_status)}
        if s.gsis_id in hurt and d["availability"] == "played":
            d["injured_during_game"] = hurt[s.gsis_id]
        out[s.actor] = d
    return out


# ---- transactions --------------------------------------------------------------------------------

def _player_points(week, player_keys):
    """Yahoo week points for arbitrary players (batched), by player_id."""
    pts = {}
    keys = list(player_keys)
    for i in range(0, len(keys), 25):
        d = api_get(f"league/{LEAGUE_KEY}/players;player_keys={','.join(keys[i:i + 25])}/stats;type=week;week={week}")
        for e in numbered(d["league"][1]["players"]):
            p = e["player"]
            pts[merge(p[0])["player_key"]] = float(merge(p[1:])["player_points"]["total"])
    return pts


def _kickoffs(sched_week):
    kick = {}
    for g in sched_week.itertuples():
        t = dt.datetime.fromisoformat(f"{g.gameday} {g.gametime}").replace(tzinfo=ET)
        kick[g.home_team] = kick[g.away_team] = t
    return kick


def transactions(week, team_keys, names, all_rosters, sched_week, res):
    r = res[res.week == week].iloc[0]
    start = dt.datetime.fromisoformat(r.week_start).replace(tzinfo=ET)
    end = dt.datetime.fromisoformat(r.week_end).replace(tzinfo=ET) + dt.timedelta(days=1)
    kick = _kickoffs(sched_week)
    first_kick = min(kick.values())

    rostered = {}
    for row in all_rosters.itertuples():
        rostered[f"{LEAGUE_KEY.split('.')[0]}.p.{row.yahoo_id}"] = row

    raw = api_get(f"league/{LEAGUE_KEY}/transactions")["league"][1]["transactions"]
    moves = []
    for e in numbered(raw):
        if len(e["transaction"]) < 2 or "players" not in e["transaction"][1]:
            continue  # e.g. commissioner settings changes with no players
        meta, body = e["transaction"][0], e["transaction"][1]
        when = dt.datetime.fromtimestamp(int(meta["timestamp"]), ET)
        if not (start <= when < end) or meta.get("status") != "successful":
            continue
        for p in numbered(body["players"]):
            pl = p["player"]
            info = merge(pl[0])
            td = pl[1]["transaction_data"]
            td = td[0] if isinstance(td, list) else td
            teams_involved = {td.get("source_team_key"), td.get("destination_team_key")}
            if not teams_involved & set(team_keys):
                continue
            nfl_team = TEAM_FIX.get(info["editorial_team_abbr"].upper(), info["editorial_team_abbr"].upper())
            ko = kick.get(nfl_team)
            moves.append({
                "when": when, "key": info["player_key"], "name": info["name"]["full"], "pos": info["display_position"],
                "nfl_team": nfl_team, "type": td["type"], "source": td.get("source_type"), "transaction_type": meta["type"],
                "team_key": td.get("destination_team_key") if td["type"] in ("add", "trade") else td.get("source_team_key"),
                "from_team_key": td.get("source_team_key"), "to_team_key": td.get("destination_team_key"),
                "player_game_started": bool(ko and when >= ko),
            })
    unknown = {m["key"] for m in moves if m["key"] not in rostered}
    extra = _player_points(week, unknown) if unknown else {}

    all_names = {**names}
    out = []
    for m in sorted(moves, key=lambda x: x["when"]):
        row = rostered.get(m["key"])
        pts = row.yahoo_pts if row is not None else extra.get(m["key"], 0.0)
        started = None
        if row is not None and row.slot not in BENCH:
            started = {"team": row.team_name, "slot": row.slot}
        rec = {
            "time_et": m["when"].strftime("%a %-I:%M %p"),
            "phase": "pre-matchup" if m["when"] < first_kick else "mid-matchup",
            "move": m["type"], "how": m["source"] if m["type"] == "add" else m["transaction_type"],
            "team": names.get(m["team_key"], all_names.get(m["team_key"], m["team_key"])),
            "player": m["name"], "pos": m["pos"], "nfl_team": m["nfl_team"],
            "points_that_week": pts,
            "started_by_end_of_week": started,
            "rostered_by_end_of_week": {"team": row.team_name, "slot": row.slot} if row is not None else None,
            "players_game_already_started": m["player_game_started"],
        }
        if m["type"] == "trade":
            rec["from"], rec["to"] = names.get(m["from_team_key"]), names.get(m["to_team_key"])
        out.append(rec)
    return out


# ---- assemble ------------------------------------------------------------------------------------

def build_context(week, team_keys, names, starters, rosters, pbp, sched_week):
    res = weekly_results(week)
    if (res[res.week == week].status != "postevent").any():
        print(f"WARNING: week {week} is not final yet; records/standings may be incomplete")
    league_keys = [merge(e["team"][0])["team_key"] for e in numbered(api_get(f"league/{LEAGUE_KEY}/teams")["league"][1]["teams"])]
    all_names = {merge(e["team"][0])["team_key"]: merge(e["team"][0])["name"]
                 for e in numbered(api_get(f"league/{LEAGUE_KEY}/teams")["league"][1]["teams"])}
    all_rosters = pd.concat([yahoo_roster(k, week) for k in league_keys], ignore_index=True)
    all_rosters["team_name"] = all_rosters.team_key.map(all_names)

    records, table = records_and_standings(res, week, team_keys, names)
    avail = availability(week, starters, pbp)
    proj_rows, _ = get_rows(week, min(_kickoffs(sched_week).values()))
    proj = starter_projections(rosters, proj_rows)
    flag = lambda r: vs_projection(r.yahoo_pts, proj.get(r.actor), r.pos)

    lineups, bench = {}, {}
    for k in team_keys:
        s = starters[starters.team_key == k]
        lineups[names[k]] = [{"slot": r.slot, "name": r.name, "pos": r.pos, "nfl_team": r.nfl_team, "points": r.yahoo_pts, **avail[r.actor],
                              # Skipped when inactive / on bye: availability already explains the result.
                              "vs_projection": flag(r) if avail[r.actor]["availability"] in ("played", "active_but_did_not_play") else None}
                             for r in s.itertuples()]
        b = rosters[(rosters.team_key == k) & rosters.slot.isin(BENCH)].sort_values("yahoo_pts", ascending=False)
        bench[names[k]] = {"total_points": round(b.yahoo_pts.sum(), 2),
                           "players": [{"slot": r.slot, "name": r.name, "pos": r.pos, "nfl_team": r.nfl_team, "points": r.yahoo_pts, "vs_projection": flag(r)}
                                       for r in b.itertuples()]}
    return {
        "records": records,
        "league_standings_after": table,
        "league_context": league_context(res, week, team_keys, names),
        "projections": projections(res, week, team_keys, names),
        "lineups": lineups,
        "bench": bench,
        "transactions": transactions(week, team_keys, names, all_rosters, sched_week, res),
    }
