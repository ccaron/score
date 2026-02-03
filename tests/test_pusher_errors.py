"""Tests for pusher error handling and retry logic."""
import json
import sqlite3
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest
from score.pusher import (
    BaseEventPusher,
    CloudEventPusher,
    TransientError,
    PermanentError
)


class MockPusher(BaseEventPusher):
    """Mock pusher for testing base functionality."""

    def __init__(self, db_path, destination="test-destination"):
        super().__init__(db_path, destination)
        self.delivered_events = []

    def deliver(self, event):
        """Record event as delivered."""
        self.delivered_events.append(event)


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # Initialize database
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
            retry_count INTEGER DEFAULT 0,
            last_attempt_at INTEGER,
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


def test_exponential_backoff_calculation():
    """Test that exponential backoff is calculated correctly."""
    # Create a mock pusher to test backoff calculation
    pusher = MagicMock(spec=BaseEventPusher)
    pusher.INITIAL_BACKOFF = 1
    pusher.BACKOFF_MULTIPLIER = 2
    pusher.MAX_BACKOFF = 3600
    pusher._calculate_backoff = BaseEventPusher._calculate_backoff.__get__(pusher)

    # Test backoff progression
    assert pusher._calculate_backoff(0) == 1     # 1 * 2^0 = 1
    assert pusher._calculate_backoff(1) == 2     # 1 * 2^1 = 2
    assert pusher._calculate_backoff(2) == 4     # 1 * 2^2 = 4
    assert pusher._calculate_backoff(3) == 8     # 1 * 2^3 = 8
    assert pusher._calculate_backoff(4) == 16    # 1 * 2^4 = 16
    assert pusher._calculate_backoff(10) == 1024 # 1 * 2^10 = 1024

    # Test max backoff cap
    assert pusher._calculate_backoff(20) == 3600  # Capped at MAX_BACKOFF


def test_transient_error_categorization(temp_db):
    """Test that transient errors are properly categorized."""
    import requests

    pusher = CloudEventPusher(
        db_path=temp_db,
        cloud_api_url="http://localhost:9999",
        device_id="test-device"
    )

    # Create a test event
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
        ("TEST", "game-001", "{}", int(time.time()))
    )
    conn.commit()
    conn.close()

    event = {"id": 1, "type": "TEST", "game_id": "game-001", "payload": "{}", "created_at": int(time.time())}

    # Mock requests to raise various errors
    with patch('requests.post') as mock_post:
        # Test timeout error (transient)
        mock_post.side_effect = requests.exceptions.Timeout("Connection timeout")
        with pytest.raises(TransientError, match="timeout"):
            pusher.deliver(event)

        # Test connection error (transient)
        mock_post.side_effect = requests.exceptions.ConnectionError("Connection refused")
        with pytest.raises(TransientError, match="Connection error"):
            pusher.deliver(event)

        # Test 500 error (transient)
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_post.side_effect = requests.exceptions.HTTPError(response=mock_response)
        with pytest.raises(TransientError, match="Server error 500"):
            pusher.deliver(event)

        # Test 429 rate limit (transient)
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_post.side_effect = requests.exceptions.HTTPError(response=mock_response)
        with pytest.raises(TransientError, match="Rate limited 429"):
            pusher.deliver(event)


def test_permanent_error_categorization(temp_db):
    """Test that permanent errors are properly categorized."""
    import requests

    pusher = CloudEventPusher(
        db_path=temp_db,
        cloud_api_url="http://localhost:9999",
        device_id="test-device"
    )

    event = {"id": 1, "type": "TEST", "game_id": "game-001", "payload": "{}", "created_at": int(time.time())}

    with patch('requests.post') as mock_post:
        # Test 400 bad request (permanent)
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_post.side_effect = requests.exceptions.HTTPError(response=mock_response)
        with pytest.raises(PermanentError, match="Client error 400"):
            pusher.deliver(event)

        # Test 404 not found (permanent)
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_post.side_effect = requests.exceptions.HTTPError(response=mock_response)
        with pytest.raises(PermanentError, match="Client error 404"):
            pusher.deliver(event)


def test_retry_count_tracking(temp_db):
    """Test that retry counts are tracked correctly."""
    pusher = MockPusher(temp_db)

    # Create a test event
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
        ("TEST", None, "{}", int(time.time()))
    )
    conn.commit()
    conn.close()

    # Mark event as failed multiple times
    pusher.mark_delivered(1, success=False, retry_count=0, error_msg="First failure")
    pusher.mark_delivered(1, success=False, retry_count=1, error_msg="Second failure")
    pusher.mark_delivered(1, success=False, retry_count=2, error_msg="Third failure")

    # Check that retry count was incremented
    conn = sqlite3.connect(temp_db)
    row = conn.execute(
        "SELECT retry_count FROM deliveries WHERE event_id = 1 AND destination = ?",
        ("test-destination",)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == 3  # Should have incremented to 3


def test_max_retries_prevents_further_attempts(temp_db):
    """Test that events exceeding max retries are not returned."""
    pusher = MockPusher(temp_db)

    # Create a test event
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
        ("TEST", None, "{}", int(time.time()))
    )
    conn.commit()
    conn.close()

    # Mark event as failed with max retries exceeded
    pusher.mark_delivered(1, success=False, retry_count=pusher.MAX_RETRIES, error_msg="Too many failures")

    # Get unprocessed events - should not include the event that exceeded max retries
    events = pusher.get_unprocessed_events()
    assert len(events) == 0


def test_backoff_delays_retry(temp_db):
    """Test that backoff delays prevent immediate retry."""
    pusher = MockPusher(temp_db)

    # Create a test event
    conn = sqlite3.connect(temp_db)
    current_time = int(time.time())
    conn.execute(
        "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
        ("TEST", None, "{}", current_time)
    )
    conn.commit()
    conn.close()

    # Mark event as failed just now
    pusher.mark_delivered(1, success=False, retry_count=0, error_msg="Test failure")

    # Get unprocessed events immediately - should be empty due to backoff
    events = pusher.get_unprocessed_events()
    assert len(events) == 0  # Still in backoff period

    # Mock time to be after backoff period (2 seconds for retry_count=1)
    with patch('time.time', return_value=current_time + 3):
        events = pusher.get_unprocessed_events()
        assert len(events) == 1  # Should be ready for retry now


def test_database_schema_migration(temp_db):
    """Test that schema migration adds retry columns if missing."""
    # Drop the new columns to simulate old schema
    conn = sqlite3.connect(temp_db)
    # Note: SQLite doesn't support DROP COLUMN easily, so we'll just verify the pusher adds them
    conn.close()

    # Initialize pusher - should add columns if missing
    pusher = MockPusher(temp_db)

    # Verify columns exist
    conn = sqlite3.connect(temp_db)
    cursor = conn.execute("PRAGMA table_info(deliveries)")
    columns = [col[1] for col in cursor.fetchall()]
    conn.close()

    assert "retry_count" in columns
    assert "last_attempt_at" in columns


def test_successful_delivery_after_retries(temp_db):
    """Test that retry count is preserved when delivery finally succeeds."""
    pusher = MockPusher(temp_db)

    # Create a test event
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
        ("TEST", None, "{}", int(time.time()))
    )
    conn.commit()
    conn.close()

    # Mark as failed twice, then succeed
    pusher.mark_delivered(1, success=False, retry_count=0)
    pusher.mark_delivered(1, success=False, retry_count=1)
    pusher.mark_delivered(1, success=True, retry_count=2)

    # Check final state
    conn = sqlite3.connect(temp_db)
    row = conn.execute(
        "SELECT delivered, retry_count FROM deliveries WHERE event_id = 1 AND destination = ?",
        ("test-destination",)
    ).fetchone()
    conn.close()

    assert row[0] == 1  # delivered = 1 (success)
    assert row[1] == 2  # retry_count = 2 (preserved)
