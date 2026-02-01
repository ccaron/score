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
    """
    if current_time is None:
        current_time = int(time.time())

    seconds = 0
    running = False
    last_update = current_time

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

    # If still running, account for current elapsed time
    if running:
        elapsed = current_time - last_update
        seconds = max(0, seconds - elapsed)
        logger.debug(f"Game is running - adjusted for {elapsed}s elapsed time")

    return {
        "seconds": seconds,
        "running": running,
        "last_update": last_update
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
