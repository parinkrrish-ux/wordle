"""
NBA Dunk Finder — tiny API server for the Wordle Baller app.

What this does, in plain terms:
1. Someone's browser asks this server: "what's the most recent dunk?"
2. This server asks NBA.com (via the nba_api library) for today's games.
3. If today has games, it looks at the most recent/live one and scans its
   play-by-play for the latest dunk.
4. If there's no dunk yet today (no games started, or no dunks yet),
   it looks backwards at previous days until it finds one.
5. It sends back a small, simple JSON answer for the Wordle page to show.

This file is meant to run on Render (or any host that can run Python).
It is NOT meant to run inside a browser — browsers can't run Python.
"""

from datetime import datetime, timedelta
from flask import Flask, jsonify
from flask_cors import CORS

from nba_api.live.nba.endpoints import scoreboard, playbyplay
from nba_api.stats.endpoints import scoreboardv3

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
    'dunk' in that description is the most reliable way to catch every dunk
    type (alley-oop, putback, breakaway, etc.) without needing to know every
    internal code NBA.com uses.
    """
    dunk_actions = [
        a for a in actions
        if "dunk" in (a.get("description") or "").lower()
    ]
    if not dunk_actions:
        return None
    # Actions are in chronological order; the last one is most recent.
    return dunk_actions[-1]


def get_games_for_date(date_str):
    """
    Get the list of game IDs that were played/scheduled on a given date
    (format: 'YYYY-MM-DD'). Returns a list of dicts with at least 'gameId'.
    Uses the stats endpoint (ScoreboardV3) since the live scoreboard only
    covers *today*.
    """
    try:
        sb = scoreboardv3.ScoreboardV3(game_date=date_str)
        games_df = sb.game_header.get_data_frame()
        return games_df.to_dict("records")
    except Exception as e:
        print(f"Error fetching games for {date_str}: {e}")
        return []


def get_todays_games():
    """Get today's games using the live scoreboard (more real-time)."""
    try:
        sb = scoreboard.ScoreBoard()
        return sb.games.get_dict()
    except Exception as e:
        print(f"Error fetching today's scoreboard: {e}")
        return []


def search_day_for_latest_dunk(game_ids):
    """
    Given a list of game IDs (from one day), check each one's play-by-play
    and return the most recent dunk found across all of them, plus which
    game it came from. Returns None if no dunk found in any of these games.
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
            # 'orderNumber' increases over the course of a game, so we can
            # use it to compare dunks across multiple games on the same day.
            if best_dunk is None or dunk.get("orderNumber", 0) > best_dunk.get("orderNumber", 0):
                best_dunk = dunk
                best_game_id = game_id

    return best_dunk, best_game_id


@app.route("/latest-dunk")
def latest_dunk():
    """
    The one endpoint your Wordle page calls.
    Always returns the most recent dunk it can find, searching as far back
    in time as needed (even months, during the off-season).
    Returns JSON like:
      { "found": true, "player": "...", "description": "...",
        "period": ..., "clock": "...", "game_id": "..." }
    """
    # Step 1: try today's live/recent games first.
    todays_games = get_todays_games()
    today_ids = [g["gameId"] for g in todays_games] if todays_games else []

    dunk, game_id = (None, None)
    if today_ids:
        dunk, game_id = search_day_for_latest_dunk(today_ids)

    # Step 2: if nothing today (no games yet, or no dunks yet, or it's the
    # off-season), walk backwards day by day until we find one. 200 days
    # comfortably covers even the longest off-season gap between the Finals
    # and the next season's first games, so this will always find *some*
    # dunk as long as the NBA has ever played a game.
    days_checked_back = 0
    check_date = datetime.utcnow()
    while dunk is None and days_checked_back < 200:
        check_date = check_date - timedelta(days=1)
        date_str = check_date.strftime("%Y-%m-%d")
        games = get_games_for_date(date_str)
        game_ids = [g["gameId"] for g in games] if games else []
        if game_ids:
            dunk, game_id = search_day_for_latest_dunk(game_ids)
        days_checked_back += 1

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
