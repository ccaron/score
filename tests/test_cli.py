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


def test_load_state_from_events_with_pause(temp_db):
    """Test that state is correctly restored when game is paused."""
    from score.cli import GameState, load_state_from_events

    with patch('score.cli.DB_PATH', temp_db):
        # Setup: Create a test state and add events
        test_state = GameState()
        base_time = int(time.time()) - 1000

        # Initial clock set to 20:00
        with patch('time.time', return_value=base_time):
            test_state.add_event("CLOCK_SET", {"seconds": 1200})

        # Game started at base_time + 10
        with patch('time.time', return_value=base_time + 10):
            test_state.add_event("GAME_STARTED")

        # Game paused 300 seconds later (5 minutes elapsed)
        with patch('time.time', return_value=base_time + 310):
            test_state.add_event("GAME_PAUSED")

        # Load state from events
        with patch('score.cli.state', GameState()) as mock_state:
            load_state_from_events()

            # Verify: Clock should be at 15:00 (1200 - 300 = 900 seconds)
            assert mock_state.seconds == 900
            assert mock_state.running is False


def test_load_state_from_events_still_running(temp_db):
    """Test that state is correctly restored when game is still running."""
    from score.cli import GameState, load_state_from_events

    with patch('score.cli.DB_PATH', temp_db):
        # Setup: Create a test state and add events
        test_state = GameState()
        base_time = int(time.time()) - 100  # Started 100 seconds ago

        # Initial clock set to 20:00
        with patch('time.time', return_value=base_time - 10):
            test_state.add_event("CLOCK_SET", {"seconds": 1200})

        # Game started
        with patch('time.time', return_value=base_time):
            test_state.add_event("GAME_STARTED")

        # Load state from events
        with patch('score.cli.state', GameState()) as mock_state:
            load_state_from_events()

            # Verify: Clock should account for ~100 seconds elapsed
            # Allow 2 second tolerance for test execution time
            assert 1098 <= mock_state.seconds <= 1102
            assert mock_state.running is True


def test_load_state_from_events_multiple_start_pause_cycles(temp_db):
    """Test state restoration with multiple start/pause cycles."""
    from score.cli import GameState, load_state_from_events

    with patch('score.cli.DB_PATH', temp_db):
        # Setup: Create a test state and add events
        test_state = GameState()
        base_time = int(time.time()) - 1000

        # Initial clock set to 20:00
        with patch('time.time', return_value=base_time):
            test_state.add_event("CLOCK_SET", {"seconds": 1200})

        # First cycle: run for 60 seconds
        with patch('time.time', return_value=base_time + 10):
            test_state.add_event("GAME_STARTED")
        with patch('time.time', return_value=base_time + 70):
            test_state.add_event("GAME_PAUSED")

        # Second cycle: run for 40 seconds
        with patch('time.time', return_value=base_time + 100):
            test_state.add_event("GAME_STARTED")
        with patch('time.time', return_value=base_time + 140):
            test_state.add_event("GAME_PAUSED")

        # Load state from events
        with patch('score.cli.state', GameState()) as mock_state:
            load_state_from_events()

            # Verify: Clock should be at 18:20 (1200 - 60 - 40 = 1100 seconds)
            assert mock_state.seconds == 1100
            assert mock_state.running is False

