"""Tests for event pusher base functionality."""

import json
import os
import sqlite3
import tempfile
import time

import pytest

from score.pusher import BaseEventPusher


class MockPusher(BaseEventPusher):
    """Mock pusher that records delivered events in memory."""

    def __init__(self, db_path, destination="mock"):
        super().__init__(db_path, destination)
        self.delivered_events = []

    def deliver(self, event):
        """Record event as delivered."""
        self.delivered_events.append({
            'id': event['id'],
            'type': event['type'],
            'payload': json.loads(event['payload']) if event['payload'] else {}
        })


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
    os.unlink(db_path)


def create_test_events(db_path, events):
    """
    Helper to create test events.

    Args:
        db_path: Path to database
        events: List of tuples (relative_time, event_type, payload_dict)
    """
    base_time = int(time.time()) - 1000
    conn = sqlite3.connect(db_path)

    for relative_time, event_type, payload in events:
        timestamp = base_time + relative_time
        conn.execute(
            "INSERT INTO events (type, payload, created_at) VALUES (?, ?, ?)",
            (event_type, json.dumps(payload), timestamp)
        )

    conn.commit()
    conn.close()


def test_basic_event_processing(temp_db):
    """Test basic event processing."""
    # Create test events
    create_test_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
        (10, "GAME_STARTED", {}),
        (70, "GAME_PAUSED", {}),
    ])

    # Create pusher and process once
    pusher = MockPusher(temp_db)
    events = pusher.get_unprocessed_events()

    assert len(events) == 3

    # Process each event
    for event in events:
        pusher.deliver(event)
        retry_count = event['retry_count'] or 0
        pusher.mark_delivered(event['id'], success=True, retry_count=retry_count)

    # Verify events were delivered
    assert len(pusher.delivered_events) == 3
    assert pusher.delivered_events[0]['type'] == 'CLOCK_SET'
    assert pusher.delivered_events[1]['type'] == 'GAME_STARTED'
    assert pusher.delivered_events[2]['type'] == 'GAME_PAUSED'

    # Verify deliveries table
    conn = sqlite3.connect(temp_db)
    deliveries = conn.execute(
        "SELECT * FROM deliveries WHERE destination=?", ("mock",)
    ).fetchall()
    conn.close()

    assert len(deliveries) == 3
    for delivery in deliveries:
        assert delivery[2] == 1  # delivered = success


def test_event_ordering(temp_db):
    """Test that events are delivered in order."""
    create_test_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
        (10, "GAME_STARTED", {}),
        (70, "GAME_PAUSED", {}),
        (100, "GAME_STARTED", {}),
        (140, "GAME_PAUSED", {}),
    ])

    pusher = MockPusher(temp_db)

    # Get all events
    events = pusher.get_unprocessed_events()
    assert len(events) == 5

    # Verify they're in order
    assert events[0]['id'] == 1
    assert events[1]['id'] == 2
    assert events[2]['id'] == 3
    assert events[3]['id'] == 4
    assert events[4]['id'] == 5


def test_no_duplicate_deliveries(temp_db):
    """Test that successfully delivered events are not reprocessed."""
    create_test_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
        (10, "GAME_STARTED", {}),
    ])

    pusher = MockPusher(temp_db)

    # First pass - process all events
    events = pusher.get_unprocessed_events()
    assert len(events) == 2

    for event in events:
        retry_count = event['retry_count'] or 0
        pusher.mark_delivered(event['id'], success=True, retry_count=retry_count)

    # Second pass - should find no events
    events = pusher.get_unprocessed_events()
    assert len(events) == 0


def test_delivery_retry_on_failure(temp_db):
    """Test that failed deliveries are retried after backoff period."""
    from unittest.mock import patch

    create_test_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
    ])

    pusher = MockPusher(temp_db)

    # First attempt - mark as failed
    events = pusher.get_unprocessed_events()
    assert len(events) == 1

    retry_count = events[0]['retry_count'] or 0
    current_time = int(time.time())

    pusher.mark_delivered(events[0]['id'], success=False, retry_count=retry_count)

    # Verify marked as failed in database
    conn = sqlite3.connect(temp_db)
    delivery = conn.execute(
        "SELECT delivered, retry_count FROM deliveries WHERE event_id=1 AND destination=?",
        ("mock",)
    ).fetchone()
    conn.close()

    assert delivery[0] == 2  # delivered = failed
    assert delivery[1] == 1  # retry_count incremented to 1

    # Immediately after - should NOT be in unprocessed (still in backoff period)
    events = pusher.get_unprocessed_events()
    assert len(events) == 0

    # Mock time to advance past backoff period (2 seconds for retry_count=1)
    with patch('time.time', return_value=current_time + 3):
        # Now should be available for retry
        events = pusher.get_unprocessed_events()
        assert len(events) == 1

        retry_count = events[0]['retry_count'] or 0
        # Mark as success
        pusher.mark_delivered(events[0]['id'], success=True, retry_count=retry_count)

    # Final attempt - should be empty now
    events = pusher.get_unprocessed_events()
    assert len(events) == 0


def test_empty_database(temp_db):
    """Test graceful handling of empty database."""
    pusher = MockPusher(temp_db)

    events = pusher.get_unprocessed_events()
    assert len(events) == 0


def test_event_payload_format(temp_db):
    """Test that JSONL output contains all required fields."""
    create_test_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
    ])

    pusher = MockPusher(temp_db)
    events = pusher.get_unprocessed_events()

    jsonl = pusher.format_event_jsonl(events[0])

    data = json.loads(jsonl)

    # Verify all required fields present
    assert 'event_id' in data
    assert 'event_type' in data
    assert 'event_payload' in data
    assert 'event_timestamp' in data

    # Verify values
    assert data['event_id'] == 1
    assert data['event_type'] == 'CLOCK_SET'
    assert data['event_payload'] == {"seconds": 1200}


def test_multiple_destinations(temp_db):
    """Test that deliveries can track multiple destinations independently."""
    create_test_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
    ])

    pusher1 = MockPusher(temp_db, destination="dest1")
    pusher2 = MockPusher(temp_db, destination="dest2")

    # Pusher1 delivers event
    events1 = pusher1.get_unprocessed_events()
    assert len(events1) == 1
    retry_count = events1[0]['retry_count'] or 0
    pusher1.mark_delivered(events1[0]['id'], success=True, retry_count=retry_count)

    # Pusher2 should still see the event (different destination)
    events2 = pusher2.get_unprocessed_events()
    assert len(events2) == 1

    # Pusher1 should not see the event anymore
    events1_again = pusher1.get_unprocessed_events()
    assert len(events1_again) == 0
