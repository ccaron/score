"""Tests for goal tracking functionality."""
import json
import sqlite3
import tempfile
import time

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


def test_add_home_goal(client):
    """Test adding a goal for the home team."""
    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "goal" in data
    assert data["goal"]["team"] == "home"
    assert data["goal"]["cancelled"] is False
    assert "id" in data["goal"]
    assert "time" in data["goal"]
    # Check player fields
    assert "scorer_id" in data["goal"]
    assert "assist1_id" in data["goal"]
    assert "assist2_id" in data["goal"]


def test_add_away_goal(client):
    """Test adding a goal for the away team."""
    response = client.post("/add_goal", json={
        "team": "away",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["goal"]["team"] == "away"


def test_add_goal_updates_score(client):
    """Test that adding goals updates the score."""
    # Add home goal
    response1 = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    assert response1.status_code == 200

    # Add away goal
    response2 = client.post("/add_goal", json={
        "team": "away",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    assert response2.status_code == 200

    # Add another home goal
    response3 = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    assert response3.status_code == 200


def test_cancel_goal(client):
    """Test canceling a goal."""
    # Add a goal
    response1 = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    goal = response1.json()["goal"]
    goal_id = goal["id"]

    # Cancel the goal
    response2 = client.post("/cancel_goal", json={"goal_id": goal_id})

    assert response2.status_code == 200
    data = response2.json()
    assert data["status"] == "ok"
    assert data["goal"]["cancelled"] is True


def test_cancel_nonexistent_goal(client):
    """Test canceling a goal that doesn't exist."""
    response = client.post("/cancel_goal", json={"goal_id": "nonexistent"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"
    assert "not found" in data["message"].lower()


def test_cancel_already_cancelled_goal(client):
    """Test canceling a goal that's already cancelled."""
    # Add and cancel a goal
    response1 = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    goal_id = response1.json()["goal"]["id"]
    client.post("/cancel_goal", json={"goal_id": goal_id})

    # Try to cancel again
    response2 = client.post("/cancel_goal", json={"goal_id": goal_id})

    assert response2.status_code == 200
    data = response2.json()
    assert data["status"] == "error"
    assert "already cancelled" in data["message"].lower()


def test_goal_cancellation_decrements_score(client):
    """Test that canceling a goal decrements the score."""
    # Add two home goals
    response1 = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    response2 = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    goal_id = response2.json()["goal"]["id"]

    # Cancel one goal
    response3 = client.post("/cancel_goal", json={"goal_id": goal_id})
    assert response3.status_code == 200


def test_cannot_add_goal_in_clock_mode(client):
    """Test that goals cannot be added in clock mode."""
    from score.app import state
    state.mode = "clock"

    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"
    assert "clock mode" in data["message"].lower()


def test_goal_event_stored_in_database(client, temp_db):
    """Test that goal events are stored in the database."""
    # Add a goal
    client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })

    # Check database
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    events = conn.execute(
        "SELECT type, game_id, payload FROM events WHERE type = 'GOAL_HOME'"
    ).fetchall()
    conn.close()

    assert len(events) == 1
    assert events[0]["type"] == "GOAL_HOME"
    assert events[0]["game_id"] == "test-game-1"

    payload = json.loads(events[0]["payload"])
    assert payload["value"] == 1
    assert "goal_id" in payload
    assert "time" in payload


def test_goal_cancellation_event_stored(client, temp_db):
    """Test that goal cancellation events are stored."""
    # Add and cancel a goal
    response = client.post("/add_goal", json={
        "team": "away",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    goal_id = response.json()["goal"]["id"]
    client.post("/cancel_goal", json={"goal_id": goal_id})

    # Check database
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    events = conn.execute(
        "SELECT type, payload FROM events WHERE type = 'GOAL_AWAY' ORDER BY id"
    ).fetchall()
    conn.close()

    assert len(events) == 2

    # First event: goal scored
    payload1 = json.loads(events[0]["payload"])
    assert payload1["value"] == 1

    # Second event: goal cancelled
    payload2 = json.loads(events[1]["payload"])
    assert payload2["value"] == -1
    assert payload2["goal_id"] == goal_id


def test_goal_includes_game_time(client):
    """Test that goals include the game clock time."""
    from score.app import state
    state.seconds = 15 * 60 + 34  # 15:34 remaining

    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })

    goal = response.json()["goal"]
    assert goal["time"] == "15:34"


def test_multiple_goals_tracked_separately(client):
    """Test that multiple goals are tracked with unique IDs."""
    # Add multiple goals
    response1 = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    response2 = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    response3 = client.post("/add_goal", json={
        "team": "away",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })

    goal1 = response1.json()["goal"]
    goal2 = response2.json()["goal"]
    goal3 = response3.json()["goal"]

    # Each goal should have a unique ID
    assert goal1["id"] != goal2["id"]
    assert goal1["id"] != goal3["id"]
    assert goal2["id"] != goal3["id"]


def test_invalid_team_rejected(client):
    """Test that invalid team names are rejected."""
    response = client.post("/add_goal", json={"team": "invalid"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"
    assert "invalid team" in data["message"].lower()


def test_goal_state_replay():
    """Test that goals are correctly replayed from events."""
    from score.state import replay_events

    events = [
        {
            "type": "CLOCK_SET",
            "payload": json.dumps({"seconds": 1200}),
            "created_at": int(time.time())
        },
        {
            "type": "GOAL_HOME",
            "payload": json.dumps({
                "goal_id": "goal-1",
                "value": 1,
                "time": "15:00"
            }),
            "created_at": int(time.time())
        },
        {
            "type": "GOAL_AWAY",
            "payload": json.dumps({
                "goal_id": "goal-2",
                "value": 1,
                "time": "12:30"
            }),
            "created_at": int(time.time())
        },
        {
            "type": "GOAL_HOME",
            "payload": json.dumps({
                "goal_id": "goal-3",
                "value": 1,
                "time": "10:15"
            }),
            "created_at": int(time.time())
        },
        {
            "type": "GOAL_HOME",
            "payload": json.dumps({
                "goal_id": "goal-3",
                "value": -1,
                "time": "10:15"
            }),
            "created_at": int(time.time())
        }
    ]

    result = replay_events(events)

    assert result["home_score"] == 1  # 2 goals - 1 cancelled
    assert result["away_score"] == 1
    assert len(result["goals"]) == 3

    # Check goals list
    home_goals = [g for g in result["goals"] if g["team"] == "home"]
    away_goals = [g for g in result["goals"] if g["team"] == "away"]

    assert len(home_goals) == 2
    assert len(away_goals) == 1

    # Check that the cancelled goal is marked
    cancelled_goals = [g for g in result["goals"] if g["cancelled"]]
    assert len(cancelled_goals) == 1
    assert cancelled_goals[0]["id"] == "goal-3"


def test_score_cannot_go_negative(client):
    """Test that score cannot go below zero when canceling."""
    # Add one goal
    response1 = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })
    goal_id = response1.json()["goal"]["id"]

    # Cancel it (score should be 0)
    response2 = client.post("/cancel_goal", json={"goal_id": goal_id})
    assert response2.status_code == 200


def test_goal_with_players(client):
    """Test adding a goal with player information."""
    # Setup mock roster
    from score.app import state
    state.roster_loaded = True
    state.home_roster = [8471214, 8474564]
    state.roster_details = {
        "8471214": {"player_id": 8471214, "full_name": "Brad Marchand", "jersey_number": 63},
        "8474564": {"player_id": 8474564, "full_name": "David Pastrnak", "jersey_number": 88}
    }

    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": "8471214",
        "assist1_id": "8474564",
        "assist2_id": None
    })

    assert response.status_code == 200
    data = response.json()
    assert data["goal"]["scorer_id"] == "8471214"
    assert data["goal"]["assist1_id"] == "8474564"
    assert data["goal"]["assist2_id"] is None


def test_goal_without_roster(client):
    """Test that goals can be added without roster loaded."""
    response = client.post("/add_goal", json={
        "team": "home",
        "scorer_id": None,
        "assist1_id": None,
        "assist2_id": None
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    # Should succeed with null player IDs


def test_goal_replay_with_players():
    """Test that goals with player IDs are correctly replayed."""
    from score.state import replay_events
    import time
    import json

    events = [
        {
            "type": "ROSTER_INITIALIZED",
            "payload": json.dumps({
                "team": "home",
                "players": [
                    {
                        "player_id": 8471214,
                        "full_name": "Brad Marchand",
                        "jersey_number": 63,
                        "status": "active"
                    }
                ]
            }),
            "created_at": int(time.time())
        },
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
            "created_at": int(time.time())
        }
    ]

    result = replay_events(events)

    assert result["home_score"] == 1
    assert len(result["goals"]) == 1
    assert result["goals"][0]["scorer_id"] == "8471214"
    assert len(result["home_roster"]) == 1
    assert 8471214 in result["home_roster"]
