"""Tests for shared game state replay logic."""
import json
import sqlite3
import tempfile
import time

import pytest


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # Initialize database with score-app schema
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            game_id TEXT,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    import os
    os.unlink(db_path)


def test_replay_events_empty_list():
    """Test replaying an empty event list."""
    from score.state import replay_events

    result = replay_events([])

    assert result["seconds"] == 0
    assert result["running"] is False
    assert "last_update" in result


def test_replay_events_clock_set():
    """Test replaying a CLOCK_SET event."""
    from score.state import replay_events

    events = [
        {
            "type": "CLOCK_SET",
            "payload": json.dumps({"seconds": 900}),
            "created_at": int(time.time())
        }
    ]

    result = replay_events(events)

    assert result["seconds"] == 900
    assert result["running"] is False


def test_replay_events_start_and_pause():
    """Test replaying GAME_STARTED and GAME_PAUSED events."""
    from score.state import replay_events

    base_time = int(time.time()) - 1000

    events = [
        {
            "type": "CLOCK_SET",
            "payload": json.dumps({"seconds": 900}),
            "created_at": base_time
        },
        {
            "type": "GAME_STARTED",
            "payload": json.dumps({}),
            "created_at": base_time + 10
        },
        {
            "type": "GAME_PAUSED",
            "payload": json.dumps({}),
            "created_at": base_time + 70  # 60 seconds elapsed
        }
    ]

    result = replay_events(events, current_time=base_time + 100)

    # Should have 840 seconds left (900 - 60)
    assert result["seconds"] == 840
    assert result["running"] is False


def test_replay_events_still_running():
    """Test replaying events when game is still running."""
    from score.state import replay_events

    base_time = int(time.time()) - 100

    events = [
        {
            "type": "CLOCK_SET",
            "payload": json.dumps({"seconds": 900}),
            "created_at": base_time
        },
        {
            "type": "GAME_STARTED",
            "payload": json.dumps({}),
            "created_at": base_time + 10
        }
    ]

    current_time = base_time + 110  # 100 seconds elapsed since start

    result = replay_events(events, current_time=current_time)

    # Should have ~800 seconds left (900 - 100)
    assert 790 <= result["seconds"] <= 810
    assert result["running"] is True


def test_replay_events_multiple_cycles():
    """Test replaying multiple start/pause cycles."""
    from score.state import replay_events

    base_time = int(time.time()) - 1000

    events = [
        {
            "type": "CLOCK_SET",
            "payload": json.dumps({"seconds": 900}),
            "created_at": base_time
        },
        # First cycle: 60 seconds
        {
            "type": "GAME_STARTED",
            "payload": json.dumps({}),
            "created_at": base_time + 10
        },
        {
            "type": "GAME_PAUSED",
            "payload": json.dumps({}),
            "created_at": base_time + 70
        },
        # Second cycle: 30 seconds
        {
            "type": "GAME_STARTED",
            "payload": json.dumps({}),
            "created_at": base_time + 100
        },
        {
            "type": "GAME_PAUSED",
            "payload": json.dumps({}),
            "created_at": base_time + 130
        }
    ]

    result = replay_events(events, current_time=base_time + 200)

    # Should have 810 seconds left (900 - 60 - 30)
    assert result["seconds"] == 810
    assert result["running"] is False


def test_replay_events_handles_dict_payload():
    """Test that replay_events handles payload as dict (not string)."""
    from score.state import replay_events

    events = [
        {
            "type": "CLOCK_SET",
            "payload": {"seconds": 1200},  # Dict instead of JSON string
            "created_at": int(time.time())
        }
    ]

    result = replay_events(events)

    assert result["seconds"] == 1200


def test_load_game_state_from_db(temp_db):
    """Test loading game state from database."""
    from score.state import load_game_state_from_db

    # Create events in database
    conn = sqlite3.connect(temp_db)
    base_time = int(time.time()) - 1000

    events = [
        (base_time, "CLOCK_SET", "game-001", {"seconds": 900}),
        (base_time + 10, "GAME_STARTED", "game-001", {}),
        (base_time + 70, "GAME_PAUSED", "game-001", {}),
    ]

    for timestamp, event_type, game_id, payload in events:
        conn.execute(
            "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
            (event_type, game_id, json.dumps(payload), timestamp)
        )
    conn.commit()
    conn.close()

    # Load state
    result = load_game_state_from_db(temp_db, "game-001")

    assert result["seconds"] == 840  # 900 - 60
    assert result["running"] is False
    assert result["num_events"] == 3


def test_load_game_state_from_db_no_events(temp_db):
    """Test loading game state when no events exist."""
    from score.state import load_game_state_from_db

    result = load_game_state_from_db(temp_db, "game-999")

    assert result["seconds"] == 0
    assert result["running"] is False
    assert result["num_events"] == 0


def test_load_game_state_from_db_filters_by_game_id(temp_db):
    """Test that only events for the specified game are loaded."""
    from score.state import load_game_state_from_db

    # Create events for multiple games
    conn = sqlite3.connect(temp_db)
    base_time = int(time.time()) - 1000

    # Game 1 events
    conn.execute(
        "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
        ("CLOCK_SET", "game-001", json.dumps({"seconds": 900}), base_time)
    )

    # Game 2 events
    conn.execute(
        "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
        ("CLOCK_SET", "game-002", json.dumps({"seconds": 1200}), base_time)
    )

    conn.commit()
    conn.close()

    # Load game 1
    result = load_game_state_from_db(temp_db, "game-001")

    assert result["seconds"] == 900  # Not 1200 from game-002
    assert result["num_events"] == 1


def test_replay_events_with_received_at_field():
    """Test that replay_events works with received_at field (cloud schema)."""
    from score.state import replay_events

    base_time = int(time.time()) - 1000

    events = [
        {
            "type": "CLOCK_SET",
            "payload": json.dumps({"seconds": 900}),
            "received_at": base_time  # Cloud schema uses received_at
        },
        {
            "type": "GAME_STARTED",
            "payload": json.dumps({}),
            "received_at": base_time + 10
        },
        {
            "type": "GAME_PAUSED",
            "payload": json.dumps({}),
            "received_at": base_time + 70
        }
    ]

    result = replay_events(events, current_time=base_time + 100)

    assert result["seconds"] == 840
    assert result["running"] is False
