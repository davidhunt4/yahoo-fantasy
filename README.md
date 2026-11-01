# yahoo-fantasy

Yahoo Fantasy Football matchup recaps are bad. They do not understand how a matchup progressed through time, managerial decision-making context, and team performance relative to the rest of the league. They also read formulaically.

This repository generates better fantasy football matchup recaps for a Yahoo Fantasy Football league. It pulls rosters and scores from Yahoo,
rebuilds every scoring play from nflverse play-by-play under the league's scoring rules, and gives an LLM
a timeline of the matchup to write from along with additional context as mentioned (see summary_data.txt).

## Setup

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the values
```

The first Yahoo call opens an OAuth flow and saves `token.json`.

## Usage

```
python matchups.py                              # list a team's matchups this season
python timeline.py WEEK TEAM_KEY                # build the data files for a matchup
python generate.py WEEK TEAM_KEY [--dry-run]    # build (if needed) and write the recap to recaps/
python projections.py [WEEK]                    # optional: snapshot Sleeper projections before kickoff
python validate_all.py                          # check computed points against Yahoo
```

`TEAM_KEY` looks like `470.l.430652.t.9`. Recaps use `openai/gpt-5.1-thinking` through the Vercel AI Gateway
by default; override with `RECAP_MODEL`.

## Layout

- `fantasy.py` – Yahoo API client
- `scoring.py`, `nfldata.py`, `players.py` – league scoring applied to nflverse plays; roster/player mapping
- `context.py` – records, standings, projections, availability, transactions
- `timeline.py` – activity timeline and slot checkpoints
- `generate.py`, `prompts/` – prompt assembly, model call, output checks
