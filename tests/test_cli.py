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

    # Initialize the test database
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
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


def create_events(db_path, events, base_time=None):
    """Helper to create events with relative timestamps.

    Args:
        db_path: Path to the test database
        events: List of tuples (relative_time, event_type, payload_dict)
                relative_time is seconds from base_time
        base_time: Optional base timestamp. Defaults to 1000 seconds ago
    """
    from score.cli import GameState

    if base_time is None:
        base_time = int(time.time()) - 1000

    with patch('score.cli.DB_PATH', db_path):
        test_state = GameState()
        for relative_time, event_type, payload in events:
            timestamp = base_time + relative_time
            with patch('time.time', return_value=timestamp):
                test_state.add_event(event_type, payload)


def load_and_get_state(db_path):
    """Load state from events and return the loaded state.

    Args:
        db_path: Path to the test database

    Returns:
        The GameState object after loading events
    """
    from score.cli import GameState, load_state_from_events

    with patch('score.cli.DB_PATH', db_path):
        state = GameState()
        with patch('score.cli.state', state):
            load_state_from_events()
        return state


def test_load_state_from_events_with_pause(temp_db):
    """Test that state is correctly restored when game is paused."""
    create_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
        (10, "GAME_STARTED", {}),
        (310, "GAME_PAUSED", {}),  # 300 seconds elapsed
    ])

    state = load_and_get_state(temp_db)

    # Clock should be at 15:00 (1200 - 300 = 900 seconds)
    assert state.seconds == 900
    assert state.running is False


def test_load_state_from_events_still_running(temp_db):
    """Test that state is correctly restored when game is still running."""
    create_events(temp_db, [
        (-10, "CLOCK_SET", {"seconds": 1200}),
        (0, "GAME_STARTED", {}),
    ], base_time=int(time.time()) - 100)  # Started 100 seconds ago

    state = load_and_get_state(temp_db)

    # Clock should account for ~100 seconds elapsed
    # Allow 2 second tolerance for test execution time
    assert 1098 <= state.seconds <= 1102
    assert state.running is True


def test_load_state_from_events_multiple_start_pause_cycles(temp_db):
    """Test state restoration with multiple start/pause cycles."""
    create_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
        # First cycle: run for 60 seconds
        (10, "GAME_STARTED", {}),
        (70, "GAME_PAUSED", {}),
        # Second cycle: run for 40 seconds
        (100, "GAME_STARTED", {}),
        (140, "GAME_PAUSED", {}),
    ])

    state = load_and_get_state(temp_db)

    # Clock should be at 18:20 (1200 - 60 - 40 = 1100 seconds)
    assert state.seconds == 1100
    assert state.running is False

