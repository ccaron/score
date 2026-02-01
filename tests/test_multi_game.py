"""Tests for multi-game state management functionality."""
import json
import sqlite3
import tempfile
import time
from unittest.mock import patch

import pytest


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # Initialize the test database with game_id column
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
    conn.execute("""
        CREATE TABLE deliveries (
            event_id INTEGER NOT NULL,
            destination TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0,
            delivered_at INTEGER,
            PRIMARY KEY (event_id, destination),
            FOREIGN KEY (event_id) REFERENCES events(id)
        )
    """)
    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    import os
    os.unlink(db_path)


def create_game_events(db_path, game_id, events, base_time=None):
    """Helper to create events for a specific game.

    Args:
        db_path: Path to the test database
        game_id: Game ID to associate events with
        events: List of tuples (relative_time, event_type, payload_dict)
        base_time: Optional base timestamp
    """
    if base_time is None:
        base_time = int(time.time()) - 1000

    conn = sqlite3.connect(db_path)
    for relative_time, event_type, payload in events:
        timestamp = base_time + relative_time
        conn.execute(
            "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
            (event_type, game_id, json.dumps(payload or {}), timestamp)
        )
    conn.commit()
    conn.close()


def test_game_state_isolation(temp_db):
    """Test that different games maintain separate state."""
    base_time = int(time.time()) - 1000

    # Game 1: Set to 15 minutes, run for 5 minutes, pause
    create_game_events(temp_db, "game-001", [
        (0, "CLOCK_SET", {"seconds": 900}),
        (10, "GAME_STARTED", {}),
        (310, "GAME_PAUSED", {}),  # 5 minutes later
    ], base_time)

    # Game 2: Set to 20 minutes, run for 2 minutes, pause
    create_game_events(temp_db, "game-002", [
        (0, "CLOCK_SET", {"seconds": 1200}),
        (20, "GAME_STARTED", {}),
        (140, "GAME_PAUSED", {}),  # 2 minutes later
    ], base_time)

    from score.app import load_game_state

    with patch('score.app.DB_PATH', temp_db):
        from score.app import state

        # Load game 1 state
        state.mode = "game-001"
        load_game_state("game-001")
        game1_seconds = state.seconds

        # Load game 2 state
        state.mode = "game-002"
        load_game_state("game-002")
        game2_seconds = state.seconds

        # Game 1 should have ~10 minutes left (900 - 300)
        assert 580 <= game1_seconds <= 620

        # Game 2 should have ~18 minutes left (1200 - 120)
        assert 1060 <= game2_seconds <= 1100


def test_switch_between_games_preserves_state(temp_db):
    """Test that switching between games preserves each game's state."""
    base_time = int(time.time()) - 100

    # Create initial state for game-001
    create_game_events(temp_db, "game-001", [
        (0, "CLOCK_SET", {"seconds": 900}),
        (10, "GAME_STARTED", {}),
        (70, "GAME_PAUSED", {}),  # Run for 60 seconds
    ], base_time)

    from score.app import load_game_state

    with patch('score.app.DB_PATH', temp_db):
        from score.app import state

        # Load game 1
        state.mode = "game-001"
        load_game_state("game-001")
        game1_time_first = state.seconds
        assert 820 <= game1_time_first <= 860  # ~840 seconds left

        # Switch to game 2 (fresh)
        state.mode = "game-002"
        state.seconds = 1200  # Initialize with 20 minutes
        state.running = False

        # Switch back to game 1
        state.mode = "game-001"
        load_game_state("game-001")
        game1_time_second = state.seconds

        # Game 1 time should be the same (give or take a second for test timing)
        assert abs(game1_time_first - game1_time_second) <= 2


def test_initial_clock_set_event_created(temp_db):
    """Test that a CLOCK_SET event is created when initializing a new game."""
    with patch('score.app.DB_PATH', temp_db):
        from score.app import state
        state.mode = "game-001"
        state.seconds = 900

        # Add initial clock set event
        state.add_event("CLOCK_SET", {"seconds": 900})

    # Verify the event was created with correct game_id
    conn = sqlite3.connect(temp_db)
    cursor = conn.execute(
        "SELECT type, game_id, payload FROM events WHERE type = 'CLOCK_SET'"
    )
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "CLOCK_SET"
    assert row[1] == "game-001"
    payload = json.loads(row[2])
    assert payload["seconds"] == 900


def test_auto_pause_on_game_switch(temp_db):
    """Test that switching away from a running game auto-pauses it."""
    base_time = int(time.time()) - 100

    # Game 1: Started but not paused
    create_game_events(temp_db, "game-001", [
        (0, "CLOCK_SET", {"seconds": 900}),
        (10, "GAME_STARTED", {}),
    ], base_time)

    with patch('score.app.DB_PATH', temp_db):
        from score.app import state
        state.mode = "game-001"
        state.running = True
        state.last_update = base_time + 10

        # Simulate auto-pause when switching
        state.running = False
        state.add_event("GAME_PAUSED")

    # Verify GAME_PAUSED event was created
    conn = sqlite3.connect(temp_db)
    cursor = conn.execute(
        "SELECT type, game_id FROM events WHERE type = 'GAME_PAUSED'"
    )
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "GAME_PAUSED"
    assert row[1] == "game-001"


def test_game_at_zero_not_reset(temp_db):
    """Test that a game at 0 seconds is not reset when switching back."""
    base_time = int(time.time()) - 1000

    # Game runs to zero
    create_game_events(temp_db, "game-001", [
        (0, "CLOCK_SET", {"seconds": 60}),
        (10, "GAME_STARTED", {}),
        (70, "GAME_PAUSED", {}),  # Run for 60 seconds, should be at 0
    ], base_time)

    from score.app import load_game_state

    with patch('score.app.DB_PATH', temp_db):
        from score.app import state
        state.mode = "game-001"

        # Load the game state
        num_events = load_game_state("game-001")

        # Should have events (not be fresh)
        assert num_events > 0
        # Time should be at or near 0
        assert state.seconds <= 1


def test_multiple_pause_resume_cycles(temp_db):
    """Test that multiple pause/resume cycles are correctly replayed."""
    base_time = int(time.time()) - 1000

    # Game with multiple pause/resume cycles
    create_game_events(temp_db, "game-001", [
        (0, "CLOCK_SET", {"seconds": 900}),
        (10, "GAME_STARTED", {}),
        (70, "GAME_PAUSED", {}),      # Run 60s, 840s left
        (80, "GAME_STARTED", {}),
        (110, "GAME_PAUSED", {}),     # Run 30s, 810s left
        (120, "GAME_STARTED", {}),
        (180, "GAME_PAUSED", {}),     # Run 60s, 750s left
    ], base_time)

    from score.app import load_game_state

    with patch('score.app.DB_PATH', temp_db):
        from score.app import state
        state.mode = "game-001"
        load_game_state("game-001")

        # Should have ~750 seconds left (900 - 60 - 30 - 60)
        assert 730 <= state.seconds <= 770
        assert state.running is False


def test_clock_mode_has_no_game_id(temp_db):
    """Test that events in clock mode have NULL game_id."""
    with patch('score.app.DB_PATH', temp_db):
        from score.app import state
        state.mode = "clock"

        # These events should have NULL game_id
        state.add_event("CLOCK_SET", {"seconds": 1200})

    # Verify the event has NULL game_id
    conn = sqlite3.connect(temp_db)
    cursor = conn.execute("SELECT game_id FROM events WHERE type = 'CLOCK_SET'")
    row = cursor.fetchone()
    conn.close()

    assert row[0] is None


def test_events_filtered_by_game_id(temp_db):
    """Test that load_game_state only loads events for the specified game."""
    base_time = int(time.time()) - 1000

    # Create events for multiple games
    create_game_events(temp_db, "game-001", [
        (0, "CLOCK_SET", {"seconds": 900}),
        (10, "GAME_STARTED", {}),
        (70, "GAME_PAUSED", {}),  # Pause after 60 seconds
    ], base_time)

    create_game_events(temp_db, "game-002", [
        (0, "CLOCK_SET", {"seconds": 1200}),
        (10, "GAME_STARTED", {}),
        (70, "GAME_PAUSED", {}),
    ], base_time)

    from score.app import load_game_state

    with patch('score.app.DB_PATH', temp_db):
        from score.app import state
        state.mode = "game-001"

        # Load only game-001
        num_events = load_game_state("game-001")

        # Should only load 3 events (CLOCK_SET, GAME_STARTED, GAME_PAUSED), not game-002's events
        assert num_events == 3
        # Game ran for 60 seconds (70 - 10), so should have 840 seconds left
        assert state.seconds == 840  # 900 - 60 = 840
