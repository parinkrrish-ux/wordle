"""
NBA Dunk Finder — tiny API server for the Wordle Baller app.

What this does, in plain terms:
1. Someone's browser asks this server: "what's the most recent dunk?"
2. This server asks NBA.com (via the nba_api library) for the most recent
   game that's been played (checking playoffs first, then regular season,
   then last season if needed — covering the off-season too).
3. It pulls that one game's play-by-play and finds the latest dunk in it.
4. It sends back a small, simple JSON answer for the Wordle page to show.

Design note: an earlier version of this script looped through individual
days going backwards in time, which could make a lot of network calls and
use too much memory on a free hosting tier. This version instead makes
ONE call to get a whole season's list of games (already sorted to find the
newest), which is much lighter and faster.

This file is meant to run on Render (or any host that can run Python).
It is NOT meant to run inside a browser — browsers can't run Python.
"""

from datetime import datetime, timedelta
from flask import Flask, jsonify
from flask_cors import CORS

from nba_api.live.nba.endpoints import scoreboard, playbyplay
from nba_api.stats.endpoints import leaguegamelog, scoreboardv3
from nba_api.stats.library.parameters import SeasonTypeAllStar, Season

app = Flask(__name__)
# CORS lets your Wordle webpage (running on a different domain) call this server.
# Without this, browsers block the request for security reasons.
CORS(app)


def find_dunk_in_actions(actions):
    """
    Look through a list of play-by-play 'actions' and return the most
    recent one that is a dunk, or None if there isn't one.

    NBA.com's live feed describes plays in a human-readable 'description'
    field, e.g. "G. Antetokounmpo 2pt Dunk (12 PTS)". Checking for the word
    'dunk' in that description catches every dunk type (alley-oop, putback,
    breakaway, etc.) without needing to know every internal code NBA.com uses.
    """
    dunk_actions = [
        a for a in actions
        if "dunk" in (a.get("description") or "").lower()
    ]
    if not dunk_actions:
        return None
    # Actions are in chronological order; the last one is most recent.
    return dunk_actions[-1]


def get_most_recent_completed_game_id():
    """
    Find the single most recently played NBA game, regardless of whether
    it was today, last week, or last season (off-season safe).

    Checks, in order:
      1. This season's playoffs (in case we're just after the Finals)
      2. This season's regular season
      3. Last season's playoffs
      4. Last season's regular season
    Stops as soon as one of these has any games, and returns the newest
    game ID from that batch. Only ever makes a small, fixed number of
    network calls (max 4) instead of looping day by day.
    """
    current_season = Season.current_season  # e.g. "2025-26"
    # Previous season string, e.g. "2025-26" -> "2024-25"
    start_year = int(current_season[:4]) - 1
    previous_season = f"{start_year}-{str(start_year + 1)[2:]}"

    season_type_pairs = [
        (current_season, SeasonTypeAllStar.playoffs),
        (current_season, SeasonTypeAllStar.regular),
        (previous_season, SeasonTypeAllStar.playoffs),
        (previous_season, SeasonTypeAllStar.regular),
    ]

    for season, season_type in season_type_pairs:
        try:
            log = leaguegamelog.LeagueGameLog(season=season, season_type_all_star=season_type)
            df = log.league_game_log.get_data_frame()
        except Exception as e:
            print(f"Error fetching game log for {season} {season_type}: {e}")
            continue

        if df is None or len(df) == 0:
            continue

        # Sort by date (and grab the latest). GAME_DATE looks like "2026-04-12".
        df_sorted = df.sort_values("GAME_DATE", ascending=False)
        newest_game_id = df_sorted.iloc[0]["GAME_ID"]
        return newest_game_id

    return None


def get_most_recent_game_id_from_last_20_days():
    """
    Backup plan, only used if the season-log lookup above finds nothing
    (e.g. a brief NBA.com hiccup). Checks the last 20 days, one at a time,
    most recent first, and stops as soon as it finds a day with games.
    Bounded to 20 calls max, so it stays light on memory even in this
    fallback path.
    """
    check_date = datetime.utcnow()
    for _ in range(20):
        date_str = check_date.strftime("%Y-%m-%d")
        try:
            sb = scoreboardv3.ScoreboardV3(game_date=date_str)
            games_df = sb.game_header.get_data_frame()
        except Exception as e:
            print(f"Error fetching games for {date_str}: {e}")
            games_df = None

        if games_df is not None and len(games_df) > 0:
            return games_df.iloc[0]["gameId"]

        check_date -= timedelta(days=1)

    return None


def get_todays_live_game_ids():
    """
    Check today's live scoreboard. If any games are in progress or already
    finished today, we prefer those over older games. Returns a list of
    game IDs, or an empty list if there's nothing today.
    """
    try:
        sb = scoreboard.ScoreBoard()
        games = sb.games.get_dict()
        return [g["gameId"] for g in games] if games else []
    except Exception as e:
        print(f"Error fetching today's scoreboard: {e}")
        return []


def find_latest_dunk_across_games(game_ids):
    """
    Given a list of game IDs, check each one's play-by-play and return the
    most recent dunk found, plus which game it came from.
    """
    best_dunk = None
    best_game_id = None

    for game_id in game_ids:
        try:
            pbp = playbyplay.PlayByPlay(game_id)
            actions = pbp.actions.get_dict()
        except Exception as e:
            print(f"Error fetching play-by-play for game {game_id}: {e}")
            continue

        dunk = find_dunk_in_actions(actions)
        if dunk is not None:
            if best_dunk is None or dunk.get("orderNumber", 0) > best_dunk.get("orderNumber", 0):
                best_dunk = dunk
                best_game_id = game_id

    return best_dunk, best_game_id


@app.route("/latest-dunk")
def latest_dunk():
    """
    The one endpoint your Wordle page calls.
    Always tries to return the most recent dunk it can find — checking
    today's games first, then falling back to the most recent completed
    game from this season or last season.
    Returns JSON like:
      { "found": true, "player": "...", "description": "...",
        "period": ..., "clock": "...", "game_id": "..." }
    """
    dunk = None
    game_id = None

    # Step 1: check today's live/finished games first, if there are any.
    today_ids = get_todays_live_game_ids()
    if today_ids:
        dunk, game_id = find_latest_dunk_across_games(today_ids)

    # Step 2: otherwise, find the single most recent completed game
    # (this season or last season) and check that.
    if dunk is None:
        recent_game_id = get_most_recent_completed_game_id()
        if recent_game_id:
            dunk, game_id = find_latest_dunk_across_games([recent_game_id])

    # Step 3: rare fallback — if the season-log lookup didn't turn up
    # anything (e.g. a brief NBA.com hiccup), check the last 20 days
    # one by one instead.
    if dunk is None:
        fallback_game_id = get_most_recent_game_id_from_last_20_days()
        if fallback_game_id:
            dunk, game_id = find_latest_dunk_across_games([fallback_game_id])

    if dunk is None:
        return jsonify({
            "found": False,
            "message": "Couldn't find a dunk right now — try again in a moment."
        }), 200

    return jsonify({
        "found": True,
        "player": dunk.get("playerNameI") or dunk.get("playerName") or "Unknown player",
        "description": dunk.get("description", ""),
        "period": dunk.get("period"),
        "clock": dunk.get("clock"),
        "game_id": game_id,
    })


@app.route("/")
def home():
    """Just a friendly message if someone visits the server's homepage directly."""
    return jsonify({
        "message": "NBA Dunk Finder is running. Try /latest-dunk to get data."
    })


if __name__ == "__main__":
    # Render sets the PORT environment variable; default to 5000 for local testing.
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
