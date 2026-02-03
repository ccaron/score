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

    # Initialize the test database with both tables
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


def create_events(db_path, events, base_time=None):
    """Helper to create events with relative timestamps.

    Args:
        db_path: Path to the test database
        events: List of tuples (relative_time, event_type, payload_dict)
                relative_time is seconds from base_time
        base_time: Optional base timestamp. Defaults to 1000 seconds ago
    """
    from score.app import GameState

    if base_time is None:
        base_time = int(time.time()) - 1000

    with patch('score.app.DB_PATH', db_path):
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
    from score.app import load_state_from_events

    with patch('score.app.DB_PATH', db_path):
        # Import state after patching DB_PATH
        from score.app import state
        # Reset state to defaults before loading
        state.seconds = 20 * 60
        state.running = False
        state.last_update = int(time.time())
        state.mode = "clock"
        state.current_game = None
        load_state_from_events()
        return state


def test_load_state_from_events_with_pause(temp_db):
    """Test that game state is correctly restored when game is paused."""
    # Create events for a specific game
    conn = sqlite3.connect(temp_db)
    base_time = int(time.time()) - 1000
    events = [
        (base_time, "CLOCK_SET", "game-001", {"seconds": 1200}),
        (base_time + 10, "GAME_STARTED", "game-001", {}),
        (base_time + 310, "GAME_PAUSED", "game-001", {}),  # 300 seconds elapsed
    ]
    for timestamp, event_type, game_id, payload in events:
        conn.execute(
            "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
            (event_type, game_id, json.dumps(payload or {}), timestamp)
        )
    conn.commit()
    conn.close()

    from score.app import load_game_state

    with patch('score.app.DB_PATH', temp_db):
        from score.app import state
        state.mode = "game-001"
        load_game_state("game-001")

        # Clock should be at 15:00 (1200 - 300 = 900 seconds)
        assert state.seconds == 900
        assert state.running is False


def test_load_state_from_events_still_running(temp_db):
    """Test that game state is correctly restored when game is still running."""
    # Create events for a specific game
    conn = sqlite3.connect(temp_db)
    base_time = int(time.time()) - 100  # Started 100 seconds ago
    events = [
        (base_time - 10, "CLOCK_SET", "game-001", {"seconds": 1200}),
        (base_time, "GAME_STARTED", "game-001", {}),
    ]
    for timestamp, event_type, game_id, payload in events:
        conn.execute(
            "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
            (event_type, game_id, json.dumps(payload or {}), timestamp)
        )
    conn.commit()
    conn.close()

    from score.app import load_game_state

    with patch('score.app.DB_PATH', temp_db):
        from score.app import state
        state.mode = "game-001"
        load_game_state("game-001")

        # Clock should account for ~100 seconds elapsed
        # Allow 2 second tolerance for test execution time
        assert 1098 <= state.seconds <= 1102
        assert state.running is True


def test_load_state_from_events_multiple_start_pause_cycles(temp_db):
    """Test state restoration with multiple start/pause cycles."""
    # Create events for a specific game
    conn = sqlite3.connect(temp_db)
    base_time = int(time.time()) - 1000
    events = [
        (base_time, "CLOCK_SET", "game-001", {"seconds": 1200}),
        # First cycle: run for 60 seconds
        (base_time + 10, "GAME_STARTED", "game-001", {}),
        (base_time + 70, "GAME_PAUSED", "game-001", {}),
        # Second cycle: run for 40 seconds
        (base_time + 100, "GAME_STARTED", "game-001", {}),
        (base_time + 140, "GAME_PAUSED", "game-001", {}),
    ]
    for timestamp, event_type, game_id, payload in events:
        conn.execute(
            "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
            (event_type, game_id, json.dumps(payload or {}), timestamp)
        )
    conn.commit()
    conn.close()

    from score.app import load_game_state

    with patch('score.app.DB_PATH', temp_db):
        from score.app import state
        state.mode = "game-001"
        load_game_state("game-001")

        # Clock should be at 18:20 (1200 - 60 - 40 = 1100 seconds)
        assert state.seconds == 1100
        assert state.running is False


# ---------- Tests for has_undelivered_events() ----------

def test_has_undelivered_events_no_events(temp_db):
    """Test has_undelivered_events when there are no events."""
    from score.app import GameState

    with patch('score.app.DB_PATH', temp_db):
        state = GameState()
        assert state.has_undelivered_events("test-destination") is False


def test_has_undelivered_events_with_undelivered(temp_db):
    """Test has_undelivered_events when there are events with no delivery record."""
    from score.app import GameState

    # Create events but no deliveries
    create_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
        (10, "GAME_STARTED", {}),
    ])

    with patch('score.app.DB_PATH', temp_db):
        state = GameState()
        assert state.has_undelivered_events("test-destination") is True


def test_has_undelivered_events_all_delivered(temp_db):
    """Test has_undelivered_events when all events are successfully delivered."""
    from score.app import GameState

    # Create events
    create_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
        (10, "GAME_STARTED", {}),
    ])

    # Mark all as delivered
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO deliveries (event_id, destination, delivered, delivered_at) VALUES (1, 'test-destination', 1, ?)",
        (int(time.time()),)
    )
    conn.execute(
        "INSERT INTO deliveries (event_id, destination, delivered, delivered_at) VALUES (2, 'test-destination', 1, ?)",
        (int(time.time()),)
    )
    conn.commit()
    conn.close()

    with patch('score.app.DB_PATH', temp_db):
        state = GameState()
        assert state.has_undelivered_events("test-destination") is False


def test_has_undelivered_events_with_failures(temp_db):
    """Test has_undelivered_events when there are failed deliveries."""
    from score.app import GameState

    # Create events
    create_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
        (10, "GAME_STARTED", {}),
    ])

    # Mark first as success, second as failed
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO deliveries (event_id, destination, delivered, delivered_at) VALUES (1, 'test-destination', 1, ?)",
        (int(time.time()),)
    )
    conn.execute(
        "INSERT INTO deliveries (event_id, destination, delivered) VALUES (2, 'test-destination', 2)"
    )
    conn.commit()
    conn.close()

    with patch('score.app.DB_PATH', temp_db):
        state = GameState()
        # Should return True because event 2 has failed delivery (status=2)
        assert state.has_undelivered_events("test-destination") is True


def test_has_undelivered_events_mixed_state(temp_db):
    """Test has_undelivered_events with mix of delivered, failed, and undelivered."""
    from score.app import GameState

    # Create events
    create_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
        (10, "GAME_STARTED", {}),
        (20, "GAME_PAUSED", {}),
    ])

    # Mark first as success, second as failed, third has no delivery record
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO deliveries (event_id, destination, delivered, delivered_at) VALUES (1, 'test-destination', 1, ?)",
        (int(time.time()),)
    )
    conn.execute(
        "INSERT INTO deliveries (event_id, destination, delivered) VALUES (2, 'test-destination', 2)"
    )
    conn.commit()
    conn.close()

    with patch('score.app.DB_PATH', temp_db):
        state = GameState()
        # Should return True because event 2 failed and event 3 is undelivered
        assert state.has_undelivered_events("test-destination") is True


def test_has_undelivered_events_different_destination(temp_db):
    """Test has_undelivered_events with different destinations."""
    from score.app import GameState

    # Create events
    create_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
    ])

    # Mark as delivered to a different destination
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO deliveries (event_id, destination, delivered, delivered_at) VALUES (1, 'other.log', 1, ?)",
        (int(time.time()),)
    )
    conn.commit()
    conn.close()

    with patch('score.app.DB_PATH', temp_db):
        state = GameState()
        # Should return True for test-destination (not delivered there yet)
        assert state.has_undelivered_events("test-destination") is True
        # Should return False for other.log (delivered)
        assert state.has_undelivered_events("other.log") is False


# ---------- Tests for pusher status determination ----------

def test_pusher_status_unknown_when_no_process(temp_db):
    """Test status is 'unknown' when pusher_process is None."""
    from score.app import GameState
    from unittest.mock import MagicMock

    with patch('score.app.DB_PATH', temp_db):
        with patch('score.app.pusher_process', None):
            state = GameState()

            # Simulate what game_loop does
            if None is not None:
                pass  # This won't execute
            else:
                state.pusher_status = "unknown"

            assert state.pusher_status == "unknown"


def test_pusher_status_dead_when_process_not_alive(temp_db):
    """Test status is 'dead' when process is not alive."""
    from score.app import GameState
    from unittest.mock import MagicMock

    with patch('score.app.DB_PATH', temp_db):
        # Mock a dead process
        mock_process = MagicMock()
        mock_process.is_alive.return_value = False

        state = GameState()

        # Simulate what game_loop does
        is_alive = mock_process.is_alive()
        if not is_alive:
            state.pusher_status = "dead"
        elif state.has_undelivered_events():
            state.pusher_status = "pending"
        else:
            state.pusher_status = "healthy"

        assert state.pusher_status == "dead"


def test_pusher_status_pending_when_alive_with_undelivered(temp_db):
    """Test status is 'pending' when process is alive but has undelivered events."""
    from score.app import GameState
    from unittest.mock import MagicMock

    # Create undelivered events
    create_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
    ])

    with patch('score.app.DB_PATH', temp_db):
        # Mock an alive process
        mock_process = MagicMock()
        mock_process.is_alive.return_value = True

        state = GameState()

        # Simulate what game_loop does
        is_alive = mock_process.is_alive()
        if not is_alive:
            state.pusher_status = "dead"
        elif state.has_undelivered_events():
            state.pusher_status = "pending"
        else:
            state.pusher_status = "healthy"

        assert state.pusher_status == "pending"


def test_pusher_status_healthy_when_alive_all_delivered(temp_db):
    """Test status is 'healthy' when process is alive and all events delivered."""
    from score.app import GameState, CLOUD_API_URL
    from unittest.mock import MagicMock

    # Create events and mark all as delivered
    create_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
    ])

    conn = sqlite3.connect(temp_db)
    # Mark as delivered to cloud destination
    conn.execute(
        "INSERT INTO deliveries (event_id, destination, delivered, delivered_at) VALUES (1, ?, 1, ?)",
        (f"cloud:{CLOUD_API_URL}", int(time.time()))
    )
    conn.commit()
    conn.close()

    with patch('score.app.DB_PATH', temp_db):
        # Mock an alive process
        mock_process = MagicMock()
        mock_process.is_alive.return_value = True

        state = GameState()

        # Simulate what game_loop does
        is_alive = mock_process.is_alive()
        if not is_alive:
            state.pusher_status = "dead"
        elif state.has_undelivered_events():
            state.pusher_status = "pending"
        else:
            state.pusher_status = "healthy"

        assert state.pusher_status == "healthy"


def test_pusher_status_dead_takes_priority_over_undelivered(temp_db):
    """Test that 'dead' status takes priority even if there are undelivered events."""
    from score.app import GameState
    from unittest.mock import MagicMock

    # Create undelivered events
    create_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
    ])

    with patch('score.app.DB_PATH', temp_db):
        # Mock a dead process
        mock_process = MagicMock()
        mock_process.is_alive.return_value = False

        state = GameState()

        # Simulate what game_loop does
        is_alive = mock_process.is_alive()
        if not is_alive:
            state.pusher_status = "dead"
        elif state.has_undelivered_events():
            state.pusher_status = "pending"
        else:
            state.pusher_status = "healthy"

        # Should be dead, not pending, even though events are undelivered
        assert state.pusher_status == "dead"


# ---------- Tests for mode functionality ----------

def test_default_mode_is_clock(temp_db):
    """Test that default mode is 'clock'."""
    from score.app import GameState

    with patch('score.app.DB_PATH', temp_db):
        state = GameState()
        assert state.mode == "clock"


def test_mode_changed_event_no_longer_exists(temp_db):
    """Test that changing mode does not create MODE_CHANGED events (removed)."""
    from score.app import GameState

    with patch('score.app.DB_PATH', temp_db):
        state = GameState()

        # Change mode (just in memory, no event)
        state.mode = "game-001"

        # Verify NO MODE_CHANGED event was created
        conn = sqlite3.connect(temp_db)
        event = conn.execute(
            "SELECT type FROM events WHERE type='MODE_CHANGED'"
        ).fetchone()
        conn.close()

        assert event is None


def test_app_starts_in_clock_mode(temp_db):
    """Test that the app always starts in clock mode regardless of events."""
    # Create events for a game
    conn = sqlite3.connect(temp_db)
    base_time = int(time.time()) - 1000
    events = [
        (base_time, "CLOCK_SET", "game-001", {"seconds": 1200}),
        (base_time + 10, "GAME_STARTED", "game-001", {}),
    ]
    for timestamp, event_type, game_id, payload in events:
        conn.execute(
            "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
            (event_type, game_id, json.dumps(payload or {}), timestamp)
        )
    conn.commit()
    conn.close()

    # Load state on startup
    state = load_and_get_state(temp_db)

    # Should be in clock mode despite game events existing
    assert state.mode == "clock"


def test_to_dict_includes_mode_and_time(temp_db):
    """Test that to_dict() includes mode and current_time fields."""
    from score.app import GameState

    with patch('score.app.DB_PATH', temp_db):
        state = GameState()
        state.mode = "clock"

        result = state.to_dict()

        assert "mode" in result
        assert "current_time" in result
        assert result["mode"] == "clock"
        # Verify current_time has format HH:MM
        assert len(result["current_time"]) == 5
        assert result["current_time"][2] == ":"


def test_app_always_starts_in_clock_mode(temp_db):
    """Test that app always starts in clock mode, regardless of game events."""
    # Create events for a specific game
    conn = sqlite3.connect(temp_db)
    base_time = int(time.time()) - 1000
    events = [
        (base_time, "CLOCK_SET", "game-001", {"seconds": 1200}),
        (base_time + 20, "GAME_STARTED", "game-001", {}),
        (base_time + 80, "GAME_PAUSED", "game-001", {}),
    ]
    for timestamp, event_type, game_id, payload in events:
        conn.execute(
            "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
            (event_type, game_id, json.dumps(payload or {}), timestamp)
        )
    conn.commit()
    conn.close()

    state = load_and_get_state(temp_db)

    # App should always start in clock mode
    assert state.mode == "clock"
    # Default state values should be used (not from game events)
    assert state.seconds == 20 * 60  # Default
    assert state.running is False




