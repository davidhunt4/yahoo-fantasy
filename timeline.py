"""Build the activity timeline for one fantasy matchup.

An activity is points scored by one of the matchup's starters (or a defense losing points-allowed
tiers). Consecutive activities by the same player, with no other starter scoring in between and the
same sign, are merged into one.

Usage: python timeline.py WEEK TEAM_KEY   (opponent is looked up from the schedule)
"""
import json
import re
import sys

import pandas as pd

from nfldata import load

from fantasy import LEAGUE_KEY, api_get, merge, numbered
from context import build_context
from players import attach_actors, yahoo_roster
from scoring import D_TD, def_pa_events, points_allowed_score, score_plays

SEASON = 2026
BENCH = ("BN", "IR")
ET = "America/New_York"
DEF_BASELINE = points_allowed_score(0)


def find_matchup(team_key, week):
    d = api_get(f"team/{team_key}/matchups")
    for e in numbered(d["team"][1]["matchups"]):
        m = e["matchup"]
        if int(m["week"]) == week:
            names, keys = {}, []
            for t in numbered(m["0"]["teams"]):
                info = merge(t["team"][0])
                names[info["team_key"]] = info["name"]
                keys.append(info["team_key"])
            return keys, names
    sys.exit(f"No matchup in week {week} for {team_key}")


def game_slot(row):
    """Classify a game by kickoff day / time (ET)."""
    day, hour = row.weekday, int(row.gametime.split(":")[0])
    if day == "Thursday":
        return "TNF"
    if day == "Monday":
        return "Monday night"
    if day == "Sunday":
        if hour < 12:
            return "Sunday early (international)"
        if hour < 15:
            return "Sunday noon"
        if hour < 19:
            return "Sunday afternoon"
        return "Sunday night"
    return "Other"


def play_times(pbp):
    """Wall-clock time (ET) per play; the ~3% of plays with no timestamp are interpolated within the game."""
    p = pbp.sort_values(["game_id", "play_id"]).copy()
    ts = pd.to_datetime(p.time_of_day, utc=True, errors="coerce")
    secs = (ts - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
    secs = secs.groupby(p.game_id).transform(lambda x: x.interpolate(limit_direction="both"))
    p["ts"] = pd.to_datetime(secs, unit="s", utc=True).dt.tz_convert(ET)
    return p.set_index(["game_id", "play_id"])[["ts", "qtr", "time", "desc", "home_team", "away_team"]]


def clean_desc(desc):
    return re.sub(r"^\(\d+:\d+\)\s*(\([^)]*\)\s*)?", "", str(desc)).strip()


def build_timeline(week, team_keys, names):
    rosters = pd.concat([attach_actors(yahoo_roster(k, week)) for k in team_keys], ignore_index=True)
    starters = rosters[~rosters.slot.isin(BENCH)]
    unmapped = starters[starters.actor.isna()]
    if len(unmapped):
        sys.exit(f"Unmapped starters: {unmapped.name.tolist()}")
    actor_info = starters.set_index("actor")[["name", "team_key", "yahoo_pts"]]

    pbp = load("load_pbp", SEASON)
    pbp = pbp[pbp.week == week]
    sched = load("load_schedules", SEASON).set_index("game_id")
    slots = {gid: game_slot(r) for gid, r in sched[sched.week == week].iterrows()}
    times = play_times(pbp)

    ev = pd.concat([score_plays(pbp), def_pa_events(pbp)], ignore_index=True)
    ev = ev[ev.actor.isin(actor_info.index)].copy()
    info = times.loc[list(zip(ev.game_id, ev.play_id))].reset_index(drop=True)
    ev = pd.concat([ev.reset_index(drop=True), info[["ts", "qtr", "time", "desc"]]], axis=1)
    ev = ev.sort_values(["ts", "play_id", "seq"]).reset_index(drop=True)

    # Merge consecutive same-player, same-sign events, except that an event which flips the lead stands
    # alone: it never merges into earlier events and nothing merges into it.
    a, b = team_keys
    score = {k: DEF_BASELINE * int(((starters.team_key == k) & (starters.pos == "DEF")).sum()) for k in team_keys}
    start_score = dict(score)
    team_of = actor_info.team_key.to_dict()
    leader, lead_changes, merged = None, 0, []
    for r in ev.itertuples():
        score[team_of[r.actor]] += r.pts
        now = a if score[a] > score[b] else b if score[b] > score[a] else None
        flips = bool(now and leader and now != leader)
        leader = now or leader
        lead_changes += flips
        snap = {"score": dict(score), "leader": now, "flips": flips}
        last = merged[-1] if merged else None
        same = bool(last) and last["actor"] == r.actor and (last["pts"] > 0) == (r.pts > 0)
        same_play = same and (r.game_id, r.play_id) in last["_keys"]
        # A player's points on a single play (yards + TD) are always one activity.
        if same_play or (same and not flips and not last["flips"]):
            snap["flips"] = flips or last["flips"]
            last["pts"] += r.pts
            last["end_ts"], last["end_clock"] = r.ts, f"Q{int(r.qtr)} {r.time}"
            last["breakdown"][r.kind] = last["breakdown"].get(r.kind, 0) + r.pts
            last.update(snap)
            if (r.game_id, r.play_id) not in last["_keys"]:
                last["_keys"].add((r.game_id, r.play_id))
                last["plays"].append(clean_desc(r.desc))
        else:
            merged.append({
                "actor": r.actor, "pts": r.pts, "game_id": r.game_id, "ts": r.ts, "end_ts": r.ts,
                "clock": f"Q{int(r.qtr)} {r.time}", "end_clock": f"Q{int(r.qtr)} {r.time}",
                "breakdown": {r.kind: r.pts}, "plays": [clean_desc(r.desc)], "_keys": {(r.game_id, r.play_id)}, **snap,
            })

    out = []
    for i, m in enumerate(merged, 1):
        end = m["end_ts"]
        out.append({
            "n": i,
            "player": actor_info.loc[m["actor"], "name"],
            "team": names[team_of[m["actor"]]],
            "points": round(m["pts"], 2),
            "score_after": {names[a]: round(m["score"][a], 2), names[b]: round(m["score"][b], 2)},
            "leader_after": names[m["leader"]] if m["leader"] else "tied",
            "lead_change": m["flips"],
            "day": end.strftime("%A"),
            "time_et": end.strftime("%-I:%M %p"),
            "slot": slots[m["game_id"]],
            "game": m["game_id"][8:],
            "game_clock": m["clock"] if m["clock"] == m["end_clock"] else f"{m['clock']} - {m['end_clock']}",
            "breakdown": {k: round(v, 2) for k, v in m["breakdown"].items()},
            "plays": m["plays"],
        })

    # Reconcile with Yahoo's totals.
    check = {names[k]: (round(score[k], 2), round(starters[starters.team_key == k].yahoo_pts.sum(), 2)) for k in team_keys}
    ctx = build_context(week, team_keys, names, starters, rosters, pbp, sched[sched.week == week])
    checkpoints = slot_checkpoints(out, names[a], names[b], {names[k]: v for k, v in start_score.items()},
                                   ctx["lineups"], sched[sched.week == week], slots)
    return {
        "week": week,
        "starting_score": {names[k]: v for k, v in start_score.items()},
        "final_score": {n: c[0] for n, c in check.items()},
        "yahoo_final_score": {n: c[1] for n, c in check.items()},
        "lead_changes": lead_changes,
        **ctx,
        "slot_checkpoints": checkpoints,
        "activities": out,
    }


PLAYING = ("played", "active_but_did_not_play")


def slot_checkpoints(out, a, b, start, lineups, sched_week, slots):
    """Scoreboard after each NFL slot. Scores count every activity from games in that slot and earlier slots."""
    first_kick, team_slot = {}, {}
    for gid, g in sched_week.iterrows():
        t = pd.Timestamp(f"{g.gameday} {g.gametime}", tz=ET)
        first_kick[slots[gid]] = min(first_kick.get(slots[gid], t), t)
        team_slot[g.home_team] = team_slot[g.away_team] = slots[gid]
    order = sorted(first_kick, key=first_kick.get)
    starters_in = lambda t, wanted: [p["name"] for p in lineups[t] if p["availability"] in PLAYING and team_slot.get(p["nfl_team"]) in wanted]

    rows, cum = [], dict(start)
    for i, slot in enumerate(order):
        playing = {t: starters_in(t, {slot}) for t in (a, b)}
        if not any(playing.values()):
            continue
        acts = [x for x in out if x["slot"] == slot]
        pts = {t: round(sum(x["points"] for x in acts if x["team"] == t), 2) for t in (a, b)}
        for t in (a, b):
            cum[t] = round(cum[t] + pts[t], 2)
        rows.append({
            "id": f"S{len(rows) + 1}", "slot": slot, "first_kickoff_et": first_kick[slot].strftime("%a %-I:%M %p"),
            "activity_ids": [f"A{acts[0]['n']}", f"A{acts[-1]['n']}", len(acts)] if acts else None,
            "points_in_slot": pts, "score_after_slot": dict(cum),
            "lead_changes_in_slot": sum(x["lead_change"] for x in acts),
            "starters_in_slot": playing, "starters_still_to_play": {t: starters_in(t, set(order[i + 1:])) for t in (a, b)},
        })
    return rows


def render_checkpoint_table(result):
    a, b = list(result["starting_score"])
    tag = team_tags([a, b])
    lines = [
        f"Teams: {tag[a]} = {a}; {tag[b]} = {b}. Score after slot counts every activity from games in that slot and earlier slots "
        f"(starting from {result['starting_score'][a]:g}-{result['starting_score'][b]:g}); slots overlap slightly, so the activity table's running score can differ mid-slot. "
        f"Activity IDs refer to rows in the activity table.",
        f"id | slot | first kickoff (ET) | activities (first-last, count) | {tag[a]} pts in slot | {tag[b]} pts in slot | "
        f"score after slot ({tag[a]}-{tag[b]}) | lead after | lead changes | {tag[a]} starters in slot | {tag[b]} starters in slot | "
        f"{tag[a]} starters still to play | {tag[b]} starters still to play",
    ]
    for r in result["slot_checkpoints"]:
        sa, sb = r["score_after_slot"][a], r["score_after_slot"][b]
        lead = "tied" if sa == sb else f"{tag[a] if sa > sb else tag[b]} +{abs(sa - sb):.2f}".rstrip("0").rstrip(".")
        ids = f"{r['activity_ids'][0]}-{r['activity_ids'][1]}, {r['activity_ids'][2]}" if r["activity_ids"] else "none"
        names_ = lambda d, t: ", ".join(d[t]) or "none"
        lines.append(" | ".join([
            r["id"], r["slot"], r["first_kickoff_et"], ids, f"{r['points_in_slot'][a]:+g}", f"{r['points_in_slot'][b]:+g}",
            f"{sa:g}-{sb:g}", lead, str(r["lead_changes_in_slot"]),
            names_(r["starters_in_slot"], a), names_(r["starters_in_slot"], b),
            names_(r["starters_still_to_play"], a), names_(r["starters_still_to_play"], b)]))
    return "\n".join(lines)


KIND_LABEL = {
    "pass_yds": "pass yds", "pass_td": "pass TD", "int_thrown": "INT thrown", "rush_yds": "rush yds", "rush_td": "rush TD",
    "rec_yds": "rec yds", "rec_td": "rec TD", "ret_td": "return TD", "2pt": "2pt", "fum_lost": "fumble lost",
    "fg": "FG", "pat": "PAT", "def_sack": "sack", "def_int": "INT", "def_fum_rec": "fumble rec", "def_td": "defensive TD",
    "def_ret_td": "return TD", "def_safety": "safety", "def_blk": "blocked kick", "def_points_allowed": "points allowed",
}


def team_tags(names):
    """Short unique tags (initials) for the table columns."""
    tags = {}
    for n in names:
        t = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", n)).upper()[:4] or "T"
        tags[n] = t if t not in tags.values() else t + str(len(tags))
    return tags


def render_activity_table(result):
    """Chronological activities as a compact pipe-delimited table (cheaper and easier to read than nested JSON)."""
    a, b = list(result["starting_score"])
    tag = team_tags([a, b])
    lines = [
        f"Teams: {tag[a]} = {a}; {tag[b]} = {b}. Scores are cumulative fantasy points for starters; each defense starts at 10 "
        f"(so both teams begin at {result['starting_score'][a]:g}-{result['starting_score'][b]:g}). Times are ET.",
        f"id | when | slot | game | game clock | player (team) | pts | {tag[a]} | {tag[b]} | lead after | what happened",
    ]
    for x in result["activities"]:
        sa, sb = x["score_after"][a], x["score_after"][b]
        if sa == sb:
            lead = "tied"
        else:
            lead = f"{tag[a] if sa > sb else tag[b]} +{abs(sa - sb):.2f}".rstrip("0").rstrip(".")
        if x["lead_change"]:
            lead += " LEAD CHANGE"
        what = ", ".join(f"{KIND_LABEL.get(k, k)} {v:g}" for k, v in x["breakdown"].items())
        plays = x["plays"]
        if "def_points_allowed" in x["breakdown"]:  # the plays are the opponent's scoring plays: list them all
            what += " | opponent scores: " + " / ".join(p[:90] for p in plays)
        else:
            what += f" ({len(plays)} plays)" if len(plays) > 2 else ""
            if len(plays) <= 2:
                what += " | " + " / ".join(p[:110] for p in plays)
        lines.append(" | ".join([
            f"A{x['n']}", f"{x['day'][:3]} {x['time_et']}", x["slot"], x["game"].replace("_", "@"), x["game_clock"],
            f"{x['player']} ({tag[x['team']]})", f"{x['points']:+g}", f"{sa:g}", f"{sb:g}", lead, what]))
    return "\n".join(lines)


def write_outputs(week, team_key):
    """Build a matchup and write its JSON + tables files; returns the file stem."""
    keys, names = find_matchup(team_key, week)
    result = build_timeline(week, keys, names)
    stem = f"timeline_wk{week}_{'_vs_'.join(k.split('.t.')[1] for k in keys)}"
    with open(f"{stem}.json", "w") as f:  # everything except the two tables
        json.dump({k: v for k, v in result.items() if k not in ("activities", "slot_checkpoints")}, f, indent=1, allow_nan=False)
    with open(f"{stem}_tables.txt", "w") as f:
        f.write("SLOT CHECKPOINTS\n" + render_checkpoint_table(result) + "\n\nACTIVITIES\n" + render_activity_table(result) + "\n")
    last = result["slot_checkpoints"][-1]["score_after_slot"]
    ok = all(abs(last[n] - result["final_score"][n]) < 0.011 for n in last)
    print(f"wrote {stem}.json + {stem}_tables.txt: {len(result['activities'])} activities, {len(result['slot_checkpoints'])} slots, {result['lead_changes']} lead changes")
    print("final", result["final_score"], "| yahoo", result["yahoo_final_score"], "| slot table ends at final score:", ok)
    return stem


if __name__ == "__main__":
    write_outputs(int(sys.argv[1]), sys.argv[2])
