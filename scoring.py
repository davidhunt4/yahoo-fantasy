"""Turn nflverse play-by-play into fantasy-point events using this league's scoring rules.

Each event is one player (or defense) earning points on one play. Actors are either a
gsis_id (players) or "DEF:<TEAM>" (team defenses).
"""
import pandas as pd

# Scoring rules from the league settings (stat_modifiers).
PASS_YD, PASS_TD, INT_THROWN = 0.04, 4, -1
RUSH_YD, RUSH_TD = 0.1, 6
REC_YD, REC_TD = 0.1, 6
RET_TD, TWO_PT, FUM_LOST = 6, 2, -2
PAT = 1
D_SACK, D_INT, D_FUM_REC, D_TD, D_SAFETY, D_BLK, D_RET_TD = 1, 2, 2, 6, 2, 2, 6


def fg_points(dist):
    return 5 if dist >= 50 else 4 if dist >= 40 else 3


def points_allowed_score(pa):
    for limit, pts in ((0, 10), (6, 7), (13, 4), (20, 1), (27, 0), (34, -1)):
        if pa <= limit:
            return pts
    return -4


def score_plays(pbp):
    """Return a DataFrame of events: game_id, play_id, actor, pts, kind, seq (order within the data)."""
    ev = []

    def add(row, actor, pts, kind):
        if pd.notna(actor) and pd.notna(pts) and pts:
            ev.append((row.game_id, row.play_id, actor, pts, kind, len(ev)))

    for r in pbp.itertuples():
        pt = r.play_type
        if pt not in ("pass", "run", "qb_kneel", "qb_spike", "field_goal", "extra_point", "kickoff", "punt"):
            continue
        defteam = f"DEF:{r.defteam}" if pd.notna(r.defteam) else None
        two_pt = r.two_point_attempt == 1

        if two_pt:
            if r.two_point_conv_result == "success":
                for a in (r.passer_player_id, r.receiver_player_id, r.rusher_player_id):
                    add(r, a, TWO_PT, "2pt")
            continue

        if pt == "pass":
            if r.sack != 1:
                add(r, r.passer_player_id, (r.passing_yards or 0) * PASS_YD, "pass_yds")
                add(r, r.receiver_player_id, (r.receiving_yards or 0) * REC_YD, "rec_yds")
            if r.lateral_reception == 1:
                add(r, r.lateral_receiver_player_id, (r.lateral_receiving_yards or 0) * REC_YD, "rec_yds")
            if r.pass_touchdown == 1:
                add(r, r.passer_player_id, PASS_TD, "pass_td")
                # On a lateral the TD belongs to whoever ends up with the ball (td_player_id)
                add(r, r.td_player_id if pd.notna(r.td_player_id) else r.receiver_player_id, REC_TD, "rec_td")
            if r.interception == 1:
                add(r, r.passer_player_id, INT_THROWN, "int_thrown")
                add(r, defteam, D_INT, "def_int")
            if r.sack == 1:
                add(r, defteam, D_SACK, "def_sack")
        elif pt in ("run", "qb_kneel"):
            add(r, r.rusher_player_id, (r.rushing_yards or 0) * RUSH_YD, "rush_yds")
            if r.lateral_rush == 1:
                add(r, r.lateral_rusher_player_id, (r.lateral_rushing_yards or 0) * RUSH_YD, "rush_yds")
            if r.rush_touchdown == 1:
                add(r, r.td_player_id if pd.notna(r.td_player_id) else r.rusher_player_id, RUSH_TD, "rush_td")

        # Defensive / return touchdowns
        if r.return_touchdown == 1 or (pd.notna(r.td_team) and r.touchdown == 1 and r.pass_touchdown != 1 and r.rush_touchdown != 1):
            if pt in ("kickoff", "punt"):
                add(r, r.td_player_id, RET_TD, "ret_td")
                add(r, f"DEF:{r.td_team}", D_RET_TD, "def_ret_td")
            elif r.td_team == r.defteam:
                add(r, f"DEF:{r.td_team}", D_TD, "def_td")

        # Fumbles lost / recovered by the defense
        for n in (1, 2):
            fumbler = getattr(r, f"fumbled_{n}_player_id")
            fteam = getattr(r, f"fumbled_{n}_team")
            rteam = getattr(r, f"fumble_recovery_{n}_team")
            if pd.notna(fumbler) and pd.notna(rteam) and rteam != fteam:
                add(r, fumbler, FUM_LOST, "fum_lost")
                add(r, f"DEF:{rteam}", D_FUM_REC, "def_fum_rec")

        if r.safety == 1:
            add(r, defteam, D_SAFETY, "def_safety")

        if pt == "field_goal":
            if r.field_goal_result == "made":
                add(r, r.kicker_player_id, fg_points(r.kick_distance), "fg")
            elif r.field_goal_result == "blocked":
                add(r, defteam, D_BLK, "def_blk")
        elif pt == "extra_point":
            if r.extra_point_result == "good":
                add(r, r.kicker_player_id, PAT, "pat")
            elif r.extra_point_result == "blocked":
                add(r, defteam, D_BLK, "def_blk")
        elif pt == "punt" and r.punt_blocked == 1:
            add(r, defteam, D_BLK, "def_blk")

    return pd.DataFrame(ev, columns=["game_id", "play_id", "actor", "pts", "kind", "seq"])


def points_allowed(pbp):
    """Final points allowed per defense: {(game_id, team): pts}.

    Yahoo charges a defense with the opponent's score minus any defensive touchdowns the
    opponent scored against it (a pick-six isn't "allowed" by the defense).
    """
    out = {}
    for gid, g in pbp.sort_values("play_id").groupby("game_id"):
        last = g.iloc[-1]
        home, away = last.home_team, last.away_team
        scores = {home: last.total_home_score, away: last.total_away_score}
        for team, opp in ((home, away), (away, home)):
            pick6 = g[(g.touchdown == 1) & (g.td_team == opp) & (g.defteam == opp) & g.play_type.isin(["pass", "run"])]
            out[(gid, team)] = scores[opp] - D_TD * len(pick6)
    return out


def def_pa_events(pbp):
    """Points-allowed activities: one event whenever an opponent score moves a defense to a lower tier.

    Every defense starts at +10 (the baseline, not an event); each event is the tier change, e.g. 10 -> 7 is -3.
    """
    ev = []
    for gid, g in pbp.sort_values("play_id").groupby("game_id"):
        home, away = g.home_team.iloc[0], g.away_team.iloc[0]
        for team, opp in ((home, away), (away, home)):
            opp_score, pick6, tier = 0, 0, points_allowed_score(0)
            for r in g.itertuples():
                if pd.notna(r.posteam):
                    post = r.posteam_score_post if r.posteam == opp else r.defteam_score_post
                    if pd.notna(post):
                        opp_score = post
                if (r.touchdown == 1 and r.td_team == opp and r.defteam == opp and r.play_type in ("pass", "run")):
                    pick6 += 1
                new = points_allowed_score(opp_score - D_TD * pick6)
                if new != tier:
                    ev.append((gid, r.play_id, f"DEF:{team}", new - tier, "def_points_allowed", 10**6 + len(ev)))
                    tier = new
    return pd.DataFrame(ev, columns=["game_id", "play_id", "actor", "pts", "kind", "seq"])
