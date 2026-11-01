"""Pull standings and the current scoreboard from a Yahoo fantasy football league."""
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.environ["YAHOO_CLIENT_ID"]
CLIENT_SECRET = os.environ["YAHOO_CLIENT_SECRET"]
REDIRECT_URI = os.getenv("YAHOO_REDIRECT_URI", "oob")
LEAGUE_KEY = os.getenv("YAHOO_LEAGUE_KEY")  # optional; defaults to your first NFL league

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"
TOKEN_FILE = Path(__file__).with_name("token.json")


# ---- auth -------------------------------------------------------------------

def _token_request(data):
    resp = requests.post(TOKEN_URL, data=data, auth=(CLIENT_ID, CLIENT_SECRET), timeout=30)
    resp.raise_for_status()
    token = resp.json()
    token["expires_at"] = time.time() + token["expires_in"] - 60
    TOKEN_FILE.write_text(json.dumps(token))
    return token


def authorize():
    """First-time login: user approves in browser and pastes back the code."""
    params = {"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "response_type": "code"}
    print("Open this URL, approve access, then copy the code (or the ?code= value in the redirect URL):\n")
    print(f"{AUTH_URL}?{urlencode(params)}\n")
    code = input("Code: ").strip()
    return _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI})


def get_access_token():
    if not TOKEN_FILE.exists():
        return authorize()["access_token"]
    token = json.loads(TOKEN_FILE.read_text())
    if time.time() >= token["expires_at"]:
        token = _token_request(
            {"grant_type": "refresh_token", "refresh_token": token["refresh_token"], "redirect_uri": REDIRECT_URI}
        )
    return token["access_token"]


# ---- api helpers ------------------------------------------------------------

def api_get(path):
    resp = requests.get(
        f"{API_BASE}/{path}",
        params={"format": "json"},
        headers={"Authorization": f"Bearer {get_access_token()}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["fantasy_content"]


def merge(list_of_dicts):
    """Yahoo returns object properties as a list of single-key dicts; merge them."""
    out = {}
    for d in list_of_dicts:
        if isinstance(d, dict):
            out.update(d)
    return out


def numbered(d):
    """Yahoo returns collections as {"0": {...}, "1": {...}, "count": N}; yield the items."""
    for i in range(int(d["count"])):
        yield d[str(i)]


# ---- queries ----------------------------------------------------------------

def find_league_key():
    data = api_get("users;use_login=1/games;game_keys=nfl/leagues")
    game = data["users"]["0"]["user"][1]["games"]["0"]["game"]
    leagues = [merge(l["league"]) for l in numbered(game[1]["leagues"])]
    if not leagues:
        sys.exit("No NFL leagues found for this account.")
    print("Your NFL leagues:")
    for l in leagues:
        print(f"  {l['league_key']}  {l['name']}")
    return leagues[0]["league_key"]


def print_standings(league_key):
    data = api_get(f"league/{league_key}/standings")
    teams = data["league"][1]["standings"][0]["teams"]
    print("\nStandings")
    for entry in numbered(teams):
        t = entry["team"]
        info = merge(t[0])
        st = merge(t[1:])["team_standings"]
        rec = st["outcome_totals"]
        print(f"  {st['rank']:>2}. {info['name']:<30} {rec['wins']}-{rec['losses']}-{rec['ties']}  PF {st['points_for']}")


def print_scoreboard(league_key):
    data = api_get(f"league/{league_key}/scoreboard")
    matchups = data["league"][1]["scoreboard"]["0"]["matchups"]
    print("\nScoreboard")
    for entry in numbered(matchups):
        m = entry["matchup"]
        side = []
        for team in numbered(m["0"]["teams"]):
            t = team["team"]
            side.append((merge(t[0])["name"], merge(t[1:])["team_points"]["total"]))
        (n1, p1), (n2, p2) = side
        print(f"  Wk {m['week']}: {n1} {p1}  vs  {n2} {p2}")


if __name__ == "__main__":
    league_key = LEAGUE_KEY or find_league_key()
    print(f"\nUsing league {league_key}")
    print_standings(league_key)
    print_scoreboard(league_key)
