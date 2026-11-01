"""List every matchup this season for one team in the league set in .env."""
import sys

from fantasy import LEAGUE_KEY, api_get, merge, numbered

TEAM_NAME = "Poop heads"

if not LEAGUE_KEY:
    sys.exit("Set YAHOO_LEAGUE_KEY in .env")

teams = api_get(f"league/{LEAGUE_KEY}/teams")["league"][1]["teams"]
infos = [merge(e["team"][0]) for e in numbered(teams)]
mine = next((i for i in infos if i["name"].lower() == TEAM_NAME.lower()), None)
if not mine:
    sys.exit(f"No team named {TEAM_NAME!r}. Teams: {', '.join(i['name'] for i in infos)}")

data = api_get(f"team/{mine['team_key']}/matchups")
matchups = data["team"][1]["matchups"]

print(f"{mine['name']} ({mine['team_key']})")
for entry in numbered(matchups):
    m = entry["matchup"]
    sides = []
    for team in numbered(m["0"]["teams"]):
        t = team["team"]
        sides.append((merge(t[0])["name"], merge(t[1:])["team_points"]["total"], merge(t[0])["team_key"]))
    (n1, p1, k1), (n2, p2, k2) = sides
    print(f"Wk {m['week']:>2} [{m['status']}] {n1} {p1} vs {n2} {p2}  winner: {m.get('winner_team_key', '-')}")
