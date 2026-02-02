"""
Shared game state replay logic.

This module provides functions to reconstruct game state by replaying events.
Used by both score-app and score-cloud.
"""
import json
import logging
import sqlite3
import time

logger = logging.getLogger("score.state")


def replay_events(events, current_time=None):
    """
    Replay a list of events to reconstruct game state.

    Args:
        events: List of event rows with 'type', 'payload', and timestamp field
                Each event should have fields accessible as dict keys
        current_time: Current timestamp for calculating elapsed time (defaults to now)

    Returns:
        dict with:
            - seconds: Time remaining in seconds
            - running: Whether clock is currently running
            - last_update: Timestamp of last state change
            - home_score: Home team score
            - away_score: Away team score
            - goals: List of goals with cancellation status
    """
    if current_time is None:
        current_time = int(time.time())

    seconds = 0
    running = False
    last_update = current_time
    home_score = 0
    away_score = 0
    goals = []  # Track goals for display

    # Roster state tracking
    home_roster = []        # List of active player IDs
    away_roster = []        # List of active player IDs
    roster_details = {}     # Map: player_id -> player info dict

    for event in events:
        # Handle different timestamp field names (created_at for score-app, received_at for cloud)
        event_time = event.get("created_at") or event.get("received_at")

        payload_str = event.get("payload", "{}")
        if isinstance(payload_str, str):
            payload = json.loads(payload_str)
        else:
            payload = payload_str

        if event["type"] == "CLOCK_SET":
            seconds = payload.get("seconds", 0)
            logger.debug(f"Replayed CLOCK_SET: {seconds}s")
        elif event["type"] == "GAME_STARTED":
            running = True
            last_update = event_time
            logger.debug("Replayed GAME_STARTED")
        elif event["type"] == "GAME_PAUSED":
            # Calculate elapsed time if was running
            if running:
                elapsed = event_time - last_update
                seconds = max(0, seconds - elapsed)
            running = False
            last_update = event_time
            logger.debug(f"Replayed GAME_PAUSED: {seconds}s remaining")
        elif event["type"] == "GOAL_HOME":
            # Goal event with value (+1 for goal, -1 for cancellation)
            goal_value = payload.get("value", 1)
            goal_id = payload.get("goal_id")
            goal_time = payload.get("time", "")

            if goal_value > 0:
                # New goal
                home_score += 1
                if goal_id:
                    goals.append({
                        "id": goal_id,
                        "team": "home",
                        "time": goal_time,
                        "cancelled": False
                    })
                logger.debug(f"Replayed GOAL_HOME (value={goal_value}): home={home_score}")
            else:
                # Goal cancellation
                home_score = max(0, home_score - 1)
                if goal_id:
                    # Mark goal as cancelled
                    for g in goals:
                        if g["id"] == goal_id:
                            g["cancelled"] = True
                            break
                logger.debug(f"Replayed GOAL_HOME cancellation (value={goal_value}): home={home_score}")

        elif event["type"] == "GOAL_AWAY":
            # Goal event with value (+1 for goal, -1 for cancellation)
            goal_value = payload.get("value", 1)
            goal_id = payload.get("goal_id")
            goal_time = payload.get("time", "")

            if goal_value > 0:
                # New goal
                away_score += 1
                if goal_id:
                    goals.append({
                        "id": goal_id,
                        "team": "away",
                        "time": goal_time,
                        "cancelled": False
                    })
                logger.debug(f"Replayed GOAL_AWAY (value={goal_value}): away={away_score}")
            else:
                # Goal cancellation
                away_score = max(0, away_score - 1)
                if goal_id:
                    # Mark goal as cancelled
                    for g in goals:
                        if g["id"] == goal_id:
                            g["cancelled"] = True
                            break
                logger.debug(f"Replayed GOAL_AWAY cancellation (value={goal_value}): away={away_score}")
        elif event["type"] == "SCORE_HOME_INC":
            # Legacy support
            home_score += 1
            logger.debug(f"Replayed SCORE_HOME_INC (legacy): home={home_score}")
        elif event["type"] == "SCORE_HOME_DEC":
            # Legacy support
            home_score = max(0, home_score - 1)
            logger.debug(f"Replayed SCORE_HOME_DEC (legacy): home={home_score}")
        elif event["type"] == "SCORE_AWAY_INC":
            # Legacy support
            away_score += 1
            logger.debug(f"Replayed SCORE_AWAY_INC (legacy): away={away_score}")
        elif event["type"] == "SCORE_AWAY_DEC":
            # Legacy support
            away_score = max(0, away_score - 1)
            logger.debug(f"Replayed SCORE_AWAY_DEC (legacy): away={away_score}")
        elif event["type"] == "SCORE_CHANGE":
            # Legacy support for old event format
            team = payload.get("team")
            score = payload.get("score")
            if team == "home":
                home_score = score
                logger.debug(f"Replayed SCORE_CHANGE (legacy): home={home_score}")
            elif team == "away":
                away_score = score
                logger.debug(f"Replayed SCORE_CHANGE (legacy): away={away_score}")

        # Roster events
        elif event["type"] == "ROSTER_INITIALIZED":
            team = payload.get("team")
            players = payload.get("players", [])

            for p in players:
                player_id = p.get("player_id")
                if player_id:
                    roster_details[player_id] = p

                    if p.get("status") == "active":
                        if team == "home" and player_id not in home_roster:
                            home_roster.append(player_id)
                        elif team == "away" and player_id not in away_roster:
                            away_roster.append(player_id)

            logger.debug(f"Replayed ROSTER_INITIALIZED: {team} ({len(players)} players)")

        elif event["type"] == "ROSTER_PLAYER_SCRATCHED":
            player_id = payload.get("player_id")
            team = payload.get("team")

            if team == "home" and player_id in home_roster:
                home_roster.remove(player_id)
            elif team == "away" and player_id in away_roster:
                away_roster.remove(player_id)

            logger.debug(f"Replayed ROSTER_PLAYER_SCRATCHED: {team} player {player_id}")

        elif event["type"] == "ROSTER_PLAYER_ACTIVATED":
            player_id = payload.get("player_id")
            team = payload.get("team")

            if team == "home" and player_id not in home_roster:
                home_roster.append(player_id)
            elif team == "away" and player_id not in away_roster:
                away_roster.append(player_id)

            logger.debug(f"Replayed ROSTER_PLAYER_ACTIVATED: {team} player {player_id}")

    # If still running, account for current elapsed time
    if running:
        elapsed = current_time - last_update
        seconds = max(0, seconds - elapsed)
        logger.debug(f"Game is running - adjusted for {elapsed}s elapsed time")

    return {
        "seconds": seconds,
        "running": running,
        "last_update": last_update,
        "home_score": home_score,
        "away_score": away_score,
        "goals": goals,
        "home_roster": home_roster,
        "away_roster": away_roster,
        "roster_details": roster_details
    }


def load_game_state_from_db(db_path, game_id):
    """
    Load game state from database by replaying events.

    Args:
        db_path: Path to SQLite database
        game_id: Game ID to load state for

    Returns:
        dict with:
            - seconds: Time remaining in seconds
            - running: Whether clock is currently running
            - last_update: Timestamp of last state change
            - num_events: Number of events replayed
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Query events - handle both score-app schema (created_at) and cloud schema (received_at)
    # Try score-app schema first
    try:
        rows = conn.execute(
            "SELECT type, payload, created_at FROM events WHERE game_id = ? ORDER BY created_at ASC",
            (game_id,)
        ).fetchall()
        logger.debug(f"Loaded {len(rows)} events from score-app schema")
    except sqlite3.OperationalError:
        # Try cloud schema
        rows = conn.execute(
            "SELECT type, payload, received_at FROM received_events WHERE game_id = ? ORDER BY seq ASC",
            (game_id,)
        ).fetchall()
        logger.debug(f"Loaded {len(rows)} events from cloud schema")

    conn.close()

    # Convert rows to dicts for replay
    events = [dict(row) for row in rows]

    state = replay_events(events)
    state["num_events"] = len(events)

    return state


def get_game_roster_at_time(db_path, game_id, target_time):
    """
    Get roster state as of a specific timestamp using temporal queries.

    Args:
        db_path: Path to database
        game_id: Game identifier
        target_time: Unix timestamp (typically game start time)

    Returns:
        dict: {
            "home_roster": [player_ids],
            "away_roster": [player_ids],
            "roster_details": {player_id: player_info}
        }
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get game info including team abbreviations
    game = conn.execute("""
        SELECT home_abbrev, away_abbrev
        FROM games
        WHERE game_id = ?
    """, (game_id,)).fetchone()

    if not game:
        conn.close()
        return {
            "home_roster": [],
            "away_roster": [],
            "roster_details": {}
        }

    home_abbrev = game["home_abbrev"]
    away_abbrev = game["away_abbrev"]

    # Temporal query for home roster
    home_players = []
    if home_abbrev:
        home_players = conn.execute("""
            SELECT p.player_id, p.full_name, p.first_name, p.last_name,
                   p.jersey_number, p.position, p.shoots_catches,
                   tr.roster_status
            FROM team_rosters tr
            JOIN players p ON tr.player_id = p.player_id
            WHERE tr.team_abbrev = ?
              AND tr.added_at <= ?
              AND (tr.removed_at IS NULL OR tr.removed_at > ?)
        """, (home_abbrev, target_time, target_time)).fetchall()

    # Temporal query for away roster
    away_players = []
    if away_abbrev:
        away_players = conn.execute("""
            SELECT p.player_id, p.full_name, p.first_name, p.last_name,
                   p.jersey_number, p.position, p.shoots_catches,
                   tr.roster_status
            FROM team_rosters tr
            JOIN players p ON tr.player_id = p.player_id
            WHERE tr.team_abbrev = ?
              AND tr.added_at <= ?
              AND (tr.removed_at IS NULL OR tr.removed_at > ?)
        """, (away_abbrev, target_time, target_time)).fetchall()

    conn.close()

    # Build roster data structures
    home_roster = []
    away_roster = []
    roster_details = {}

    for p in home_players:
        player_id = p["player_id"]
        home_roster.append(player_id)
        roster_details[player_id] = dict(p)

    for p in away_players:
        player_id = p["player_id"]
        away_roster.append(player_id)
        roster_details[player_id] = dict(p)

    return {
        "home_roster": home_roster,
        "away_roster": away_roster,
        "roster_details": roster_details
    }
