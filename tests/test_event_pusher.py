import json
import os
import sqlite3
import tempfile
import time

import pytest

from score.event_pusher import FileEventPusher


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


@pytest.fixture
def temp_output():
    """Create a temporary output file."""
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        output_path = f.name

    yield output_path

    # Cleanup
    if os.path.exists(output_path):
        os.unlink(output_path)


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


def test_basic_event_processing(temp_db, temp_output):
    """Test basic event processing and JSONL output."""
    # Create test events
    create_test_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
        (10, "GAME_STARTED", {}),
        (70, "GAME_PAUSED", {}),
    ])

    # Create pusher and process once
    pusher = FileEventPusher(temp_db, temp_output)
    events = pusher.get_unprocessed_events()

    assert len(events) == 3

    # Process each event
    for event in events:
        pusher.deliver(event)
        pusher.mark_delivered(event['id'], success=True)

    # Verify output file
    with open(temp_output) as f:
        lines = f.readlines()

    assert len(lines) == 3

    # Verify JSONL format
    event1 = json.loads(lines[0])
    assert event1['event_id'] == 1
    assert event1['event_type'] == 'CLOCK_SET'
    assert event1['event_payload'] == {"seconds": 1200}
    assert 'event_timestamp' in event1

    event2 = json.loads(lines[1])
    assert event2['event_id'] == 2
    assert event2['event_type'] == 'GAME_STARTED'
    assert event2['event_payload'] == {}

    event3 = json.loads(lines[2])
    assert event3['event_id'] == 3
    assert event3['event_type'] == 'GAME_PAUSED'
    assert event3['event_payload'] == {}

    # Verify deliveries table
    conn = sqlite3.connect(temp_db)
    deliveries = conn.execute(
        "SELECT * FROM deliveries WHERE destination=?", (temp_output,)
    ).fetchall()
    conn.close()

    assert len(deliveries) == 3
    for delivery in deliveries:
        assert delivery[2] == 1  # delivered = success


def test_game_state_reconstruction(temp_db, temp_output):
    """Test that raw events are delivered in order."""
    create_test_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
        (10, "GAME_STARTED", {}),
        (70, "GAME_PAUSED", {}),
        (100, "GAME_STARTED", {}),
        (140, "GAME_PAUSED", {}),
    ])

    pusher = FileEventPusher(temp_db, temp_output)

    # Get all events
    events = pusher.get_unprocessed_events()
    assert len(events) == 5

    # Verify they're in order
    assert events[0]['id'] == 1
    assert events[1]['id'] == 2
    assert events[2]['id'] == 3
    assert events[3]['id'] == 4
    assert events[4]['id'] == 5


def test_no_duplicate_deliveries(temp_db, temp_output):
    """Test that successfully delivered events are not reprocessed."""
    create_test_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
        (10, "GAME_STARTED", {}),
    ])

    pusher = FileEventPusher(temp_db, temp_output)

    # First pass - process all events
    events = pusher.get_unprocessed_events()
    assert len(events) == 2

    for event in events:
        pusher.mark_delivered(event['id'], success=True)

    # Second pass - should find no events
    events = pusher.get_unprocessed_events()
    assert len(events) == 0


def test_delivery_retry_on_failure(temp_db, temp_output):
    """Test that failed deliveries are retried."""
    create_test_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
    ])

    pusher = FileEventPusher(temp_db, temp_output)

    # First attempt - mark as failed
    events = pusher.get_unprocessed_events()
    assert len(events) == 1

    pusher.mark_delivered(events[0]['id'], success=False)

    # Verify marked as failed in database
    conn = sqlite3.connect(temp_db)
    delivery = conn.execute(
        "SELECT delivered FROM deliveries WHERE event_id=1 AND destination=?",
        (temp_output,)
    ).fetchone()
    conn.close()

    assert delivery[0] == 2  # delivered = failed

    # Second attempt - should still be in unprocessed
    events = pusher.get_unprocessed_events()
    assert len(events) == 1

    # Mark as success
    pusher.mark_delivered(events[0]['id'], success=True)

    # Third attempt - should be empty now
    events = pusher.get_unprocessed_events()
    assert len(events) == 0


def test_empty_database(temp_db, temp_output):
    """Test graceful handling of empty database."""
    pusher = FileEventPusher(temp_db, temp_output)

    events = pusher.get_unprocessed_events()
    assert len(events) == 0


def test_event_payload_format(temp_db, temp_output):
    """Test that JSONL output contains all required fields."""
    create_test_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
    ])

    pusher = FileEventPusher(temp_db, temp_output)
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


def test_multiple_destinations(temp_db, temp_output):
    """Test that deliveries can track multiple destinations independently."""
    create_test_events(temp_db, [
        (0, "CLOCK_SET", {"seconds": 1200}),
    ])

    pusher1 = FileEventPusher(temp_db, temp_output, destination="dest1")
    pusher2 = FileEventPusher(temp_db, temp_output + ".2", destination="dest2")

    # Pusher1 delivers event
    events1 = pusher1.get_unprocessed_events()
    assert len(events1) == 1
    pusher1.mark_delivered(events1[0]['id'], success=True)

    # Pusher2 should still see the event (different destination)
    events2 = pusher2.get_unprocessed_events()
    assert len(events2) == 1

    # Pusher1 should not see the event anymore
    events1_again = pusher1.get_unprocessed_events()
    assert len(events1_again) == 0
