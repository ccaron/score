"""Tests for goal edit functionality."""
import json
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # Initialize app database schema
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


@pytest.fixture
def client(temp_db, monkeypatch):
    """Create test client with temp database."""
    from score import app as app_module
    monkeypatch.setattr(app_module, "DB_PATH", temp_db)

    # Reinitialize app state
    from score.app import app, state
    state.mode = "test-game-1"
    state.current_game = {
        "game_id": "test-game-1",
        "home_team": "Bruins",
        "away_team": "Canadiens",
        "period_length_min": 20
    }
    state.home_score = 0
    state.away_score = 0
    state.goals = []
    # Initialize roster state
    state.home_roster = []
    state.away_roster = []
    state.roster_details = {}
    state.roster_loaded = False

    return TestClient(app)


def test_edit_goal_scorer(client):
    """Test editing a goal's scorer."""
    # Setup roster
    from score.app import state
    state.home_roster = [8471214, 8474564]
    state.roster_loaded = True

    # Add a goal with one scorer
    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": 8471214,
        "assist1_id": None,
        "assist2_id": None
    })
    assert response.status_code == 200
    goal_id = response.json()["goal"]["id"]

    # Edit the scorer
    response = client.post("/edit_goal", json={
        "goal_id": goal_id,
        "scorer_id": 8474564
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["goal"]["scorer_id"] == "8474564"
    # Assists should remain unchanged
    assert data["goal"]["assist1_id"] is None
    assert data["goal"]["assist2_id"] is None


def test_edit_goal_assists(client):
    """Test editing goal assists."""
    # Setup roster
    from score.app import state
    state.home_roster = [8471214, 8474564, 8475791]
    state.roster_loaded = True

    # Add a goal with no assists
    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": 8471214,
        "assist1_id": None,
        "assist2_id": None
    })
    goal_id = response.json()["goal"]["id"]

    # Edit to add assists
    response = client.post("/edit_goal", json={
        "goal_id": goal_id,
        "assist1_id": 8474564,
        "assist2_id": 8475791
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["goal"]["assist1_id"] == "8474564"
    assert data["goal"]["assist2_id"] == "8475791"
    # Scorer should remain unchanged
    assert data["goal"]["scorer_id"] == "8471214"


def test_edit_goal_time(client):
    """Test editing goal time."""
    # Add a goal
    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    goal_id = response.json()["goal"]["id"]
    original_time = response.json()["goal"]["time"]

    # Edit the time
    response = client.post("/edit_goal", json={
        "goal_id": goal_id,
        "time": "12:34"
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["goal"]["time"] == "12:34"
    assert data["goal"]["time"] != original_time


def test_edit_goal_clear_assists(client):
    """Test clearing goal assists."""
    # Setup roster
    from score.app import state
    state.home_roster = [8471214, 8474564, 8475791]
    state.roster_loaded = True

    # Add a goal with assists
    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": 8471214,
        "assist1_id": 8474564,
        "assist2_id": 8475791
    })
    goal_id = response.json()["goal"]["id"]

    # Edit to clear assists (set to None)
    response = client.post("/edit_goal", json={
        "goal_id": goal_id,
        "assist1_id": None,
        "assist2_id": None
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["goal"]["assist1_id"] is None
    assert data["goal"]["assist2_id"] is None
    # Scorer should remain unchanged
    assert data["goal"]["scorer_id"] == "8471214"


def test_edit_goal_not_found(client):
    """Test editing nonexistent goal returns error."""
    response = client.post("/edit_goal", json={
        "goal_id": "nonexistent",
        "scorer_id": 12345
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"
    assert "not found" in data["message"].lower()


def test_edit_goal_invalid_player(client):
    """Test editing with player not on roster returns error."""
    # Setup home roster
    from score.app import state
    state.home_roster = [8471214, 8474564]
    state.roster_loaded = True

    # Add home goal
    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": 8471214,
        "assist1_id": None,
        "assist2_id": None
    })
    goal_id = response.json()["goal"]["id"]

    # Try to set scorer to player not on roster
    response = client.post("/edit_goal", json={
        "goal_id": goal_id,
        "scorer_id": 9999999  # Not on roster
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"
    assert "roster" in data["message"].lower()


def test_edit_goal_wrong_team_roster(client):
    """Test that player must be from correct team's roster."""
    # Setup both rosters
    from score.app import state
    state.home_roster = [8471214, 8474564]
    state.away_roster = [8476459, 8477934]
    state.roster_loaded = True

    # Add home goal
    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": 8471214,
        "assist1_id": None,
        "assist2_id": None
    })
    goal_id = response.json()["goal"]["id"]

    # Try to set scorer to away player
    response = client.post("/edit_goal", json={
        "goal_id": goal_id,
        "scorer_id": 8476459  # Away player
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"
    assert "roster" in data["message"].lower()


def test_edit_goal_in_clock_mode(client):
    """Test that goals cannot be edited in clock mode."""
    from score.app import state

    # Add a goal in game mode
    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    goal_id = response.json()["goal"]["id"]

    # Switch to clock mode
    state.mode = "clock"

    # Try to edit
    response = client.post("/edit_goal", json={
        "goal_id": goal_id,
        "scorer_id": 12345
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"
    assert "clock mode" in data["message"].lower()


def test_edit_goal_without_roster(client):
    """Test that goals can be edited without roster loaded."""
    # Add a goal without roster
    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    goal_id = response.json()["goal"]["id"]

    # Edit should succeed without validation
    response = client.post("/edit_goal", json={
        "goal_id": goal_id,
        "scorer_id": 8471214
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["goal"]["scorer_id"] == "8471214"


def test_edit_cancelled_goal(client):
    """Test that cancelled goals can still be edited."""
    # Setup roster
    from score.app import state
    state.home_roster = [8471214, 8474564]
    state.roster_loaded = True

    # Add and cancel goal
    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": 8471214,
        "assist1_id": None,
        "assist2_id": None
    })
    goal_id = response.json()["goal"]["id"]

    client.post("/cancel_goal", json={"goal_id": goal_id})

    # Edit the cancelled goal
    response = client.post("/edit_goal", json={
        "goal_id": goal_id,
        "scorer_id": 8474564
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["goal"]["scorer_id"] == "8474564"
    assert data["goal"]["cancelled"] is True


def test_edit_goal_event_stored(client, temp_db):
    """Test that GOAL_EDIT event is stored in database."""
    # Add a goal
    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    goal_id = response.json()["goal"]["id"]

    # Edit the goal
    client.post("/edit_goal", json={
        "goal_id": goal_id,
        "time": "10:00"
    })

    # Check database
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    events = conn.execute("SELECT * FROM events WHERE type = 'GOAL_EDIT'").fetchall()
    conn.close()

    assert len(events) == 1
    event = events[0]
    assert event["type"] == "GOAL_EDIT"
    payload = json.loads(event["payload"])
    assert payload["goal_id"] == goal_id
    assert payload["time"] == "10:00"


def test_edit_goal_replay():
    """Test that GOAL_EDIT events are correctly replayed."""
    from score.state import replay_events
    import time

    current_time = int(time.time())

    events = [
        {
            "type": "GOAL_HOME",
            "payload": json.dumps({
                "goal_id": "goal-1",
                "value": 1,
                "time": "15:00",
                "scorer_id": "8471214",
                "assist1_id": None,
                "assist2_id": None
            }),
            "created_at": current_time
        },
        {
            "type": "GOAL_EDIT",
            "payload": json.dumps({
                "goal_id": "goal-1",
                "scorer_id": "8474564"
            }),
            "created_at": current_time + 1
        }
    ]

    result = replay_events(events)

    assert result["home_score"] == 1
    assert len(result["goals"]) == 1
    assert result["goals"][0]["scorer_id"] == "8474564"  # Updated
    assert result["goals"][0]["time"] == "15:00"  # Unchanged


def test_edit_goal_multiple_edits():
    """Test multiple edits to same goal."""
    from score.state import replay_events
    import time

    current_time = int(time.time())

    events = [
        {
            "type": "GOAL_HOME",
            "payload": json.dumps({
                "goal_id": "goal-1",
                "value": 1,
                "time": "15:00",
                "scorer_id": "8471214",
                "assist1_id": None,
                "assist2_id": None
            }),
            "created_at": current_time
        },
        {
            "type": "GOAL_EDIT",
            "payload": json.dumps({
                "goal_id": "goal-1",
                "assist1_id": "8474564"
            }),
            "created_at": current_time + 1
        },
        {
            "type": "GOAL_EDIT",
            "payload": json.dumps({
                "goal_id": "goal-1",
                "assist2_id": "8475791"
            }),
            "created_at": current_time + 2
        }
    ]

    result = replay_events(events)

    # Verify final state reflects all edits
    assert len(result["goals"]) == 1
    assert result["goals"][0]["scorer_id"] == "8471214"  # Unchanged
    assert result["goals"][0]["assist1_id"] == "8474564"  # First edit
    assert result["goals"][0]["assist2_id"] == "8475791"  # Second edit


def test_edit_goal_partial_update():
    """Test that only specified fields are changed."""
    from score.state import replay_events
    import time

    current_time = int(time.time())

    events = [
        {
            "type": "GOAL_HOME",
            "payload": json.dumps({
                "goal_id": "goal-1",
                "value": 1,
                "time": "15:00",
                "scorer_id": "8471214",
                "assist1_id": "8474564",
                "assist2_id": "8475791"
            }),
            "created_at": current_time
        },
        {
            "type": "GOAL_EDIT",
            "payload": json.dumps({
                "goal_id": "goal-1",
                "scorer_id": "9999999"  # Only change scorer
            }),
            "created_at": current_time + 1
        }
    ]

    result = replay_events(events)

    # Verify only scorer changed, assists unchanged
    assert result["goals"][0]["scorer_id"] == "9999999"  # Changed
    assert result["goals"][0]["assist1_id"] == "8474564"  # Unchanged
    assert result["goals"][0]["assist2_id"] == "8475791"  # Unchanged
    assert result["goals"][0]["time"] == "15:00"  # Unchanged


def test_edit_goal_missing_goal_id(client):
    """Test that goal_id is required."""
    response = client.post("/edit_goal", json={
        "scorer_id": 12345
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"
    assert "goal_id" in data["message"].lower()
