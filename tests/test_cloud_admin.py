"""Tests for cloud admin endpoints (device management, rinks, etc)."""
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

    # Initialize cloud database schema
    conn = sqlite3.connect(db_path)

    # Rinks table
    conn.execute("""
        CREATE TABLE rinks (
            rink_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)

    # Leagues table
    conn.execute("""
        CREATE TABLE leagues (
            league_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            league_type TEXT,
            description TEXT,
            website TEXT,
            logo_url TEXT,
            created_at INTEGER NOT NULL
        )
    """)

    # Seasons table
    conn.execute("""
        CREATE TABLE seasons (
            season_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT,
            created_at INTEGER NOT NULL
        )
    """)

    # Divisions table
    conn.execute("""
        CREATE TABLE divisions (
            division_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            division_type TEXT,
            parent_division_id TEXT,
            description TEXT,
            created_at INTEGER NOT NULL
        )
    """)

    # Team registrations table
    conn.execute("""
        CREATE TABLE team_registrations (
            registration_id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            league_id TEXT,
            season_id TEXT,
            tournament_id TEXT,
            division_id TEXT NOT NULL,
            registered_at INTEGER NOT NULL,
            withdrawn_at INTEGER
        )
    """)

    # Devices table
    conn.execute("""
        CREATE TABLE devices (
            device_id TEXT PRIMARY KEY,
            rink_id TEXT,
            sheet_name TEXT,
            device_name TEXT,
            is_assigned INTEGER DEFAULT 0,
            first_seen_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            notes TEXT,
            FOREIGN KEY (rink_id) REFERENCES rinks(rink_id)
        )
    """)

    # Games table
    conn.execute("""
        CREATE TABLE games (
            game_id TEXT PRIMARY KEY,
            rink_id TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            home_abbrev TEXT,
            away_abbrev TEXT,
            home_registration_id TEXT,
            away_registration_id TEXT,
            start_time TEXT NOT NULL,
            period_length_min INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (rink_id) REFERENCES rinks(rink_id)
        )
    """)

    # Received events table
    conn.execute("""
        CREATE TABLE received_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            event_id TEXT NOT NULL UNIQUE,
            seq INTEGER NOT NULL,
            type TEXT NOT NULL,
            ts_local TEXT NOT NULL,
            payload TEXT NOT NULL,
            received_at INTEGER NOT NULL,
            FOREIGN KEY (game_id) REFERENCES games(game_id)
        )
    """)

    # Schedule versions table
    conn.execute("""
        CREATE TABLE schedule_versions (
            rink_id TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (rink_id) REFERENCES rinks(rink_id)
        )
    """)

    # Players table
    conn.execute("""
        CREATE TABLE players (
            player_id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            jersey_number INTEGER,
            position TEXT,
            shoots_catches TEXT,
            height_inches INTEGER,
            weight_pounds INTEGER,
            birth_date TEXT,
            birth_city TEXT,
            birth_country TEXT,
            created_at INTEGER NOT NULL
        )
    """)

    # Teams table
    conn.execute("""
        CREATE TABLE teams (
            team_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            city TEXT,
            abbreviation TEXT,
            team_type TEXT,
            logo_url TEXT,
            primary_color TEXT,
            secondary_color TEXT,
            created_at INTEGER NOT NULL
        )
    """)

    # Roster entries table
    conn.execute("""
        CREATE TABLE roster_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            registration_id TEXT NOT NULL,
            player_id INTEGER NOT NULL,
            jersey_number INTEGER,
            position TEXT,
            roster_status TEXT DEFAULT 'active',
            is_captain INTEGER DEFAULT 0,
            is_alternate INTEGER DEFAULT 0,
            added_at INTEGER NOT NULL,
            removed_at INTEGER,
            FOREIGN KEY (registration_id) REFERENCES team_registrations(registration_id),
            FOREIGN KEY (player_id) REFERENCES players(player_id)
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
    # Patch the database path
    from score import cloud
    monkeypatch.setattr(cloud, "CLOUD_DB_PATH", temp_db)

    # Reinitialize the app with the new database path
    from score.cloud import app
    return TestClient(app)


def test_create_rink(client):
    """Test creating a new rink."""
    response = client.post("/admin/rinks", json={
        "rink_id": "rink-test",
        "name": "Test Arena"
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["rink"]["rink_id"] == "rink-test"
    assert data["rink"]["name"] == "Test Arena"


def test_create_duplicate_rink(client):
    """Test that creating duplicate rink fails."""
    # Create first rink
    client.post("/admin/rinks", json={
        "rink_id": "rink-test",
        "name": "Test Arena"
    })

    # Try to create duplicate
    response = client.post("/admin/rinks", json={
        "rink_id": "rink-test",
        "name": "Another Arena"
    })

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_device_auto_registration(client):
    """Test that devices auto-register on first config request."""
    response = client.get("/v1/devices/dev-abc123/config")

    assert response.status_code == 200
    data = response.json()
    assert data["device_id"] == "dev-abc123"
    assert data["is_assigned"] is False
    assert "device registered" in data["message"].lower()


def test_assign_device(client):
    """Test assigning a device to a rink and sheet."""
    # Create a rink first
    client.post("/admin/rinks", json={
        "rink_id": "rink-alpha",
        "name": "Alpha Arena"
    })

    # Auto-register device
    client.get("/v1/devices/dev-abc123/config")

    # Assign device
    response = client.put("/admin/devices/dev-abc123", json={
        "rink_id": "rink-alpha",
        "sheet_name": "Sheet 1",
        "device_name": "Main Display",
        "notes": "Test device"
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["device"]["is_assigned"] is True
    assert data["device"]["rink_id"] == "rink-alpha"
    assert data["device"]["sheet_name"] == "Sheet 1"


def test_assign_device_to_nonexistent_rink(client):
    """Test that assigning to non-existent rink fails."""
    # Auto-register device
    client.get("/v1/devices/dev-abc123/config")

    # Try to assign to non-existent rink
    response = client.put("/admin/devices/dev-abc123", json={
        "rink_id": "rink-nonexistent",
        "sheet_name": "Sheet 1"
    })

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_unassign_device(client):
    """Test unassigning a device."""
    # Create rink and assign device
    client.post("/admin/rinks", json={
        "rink_id": "rink-alpha",
        "name": "Alpha Arena"
    })
    client.get("/v1/devices/dev-abc123/config")
    client.put("/admin/devices/dev-abc123", json={
        "rink_id": "rink-alpha",
        "sheet_name": "Sheet 1"
    })

    # Unassign
    response = client.delete("/admin/devices/dev-abc123/assignment")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    # Verify unassigned
    config = client.get("/v1/devices/dev-abc123/config")
    assert config.json()["is_assigned"] is False


def test_delete_device(client):
    """Test deleting a device."""
    # Auto-register device
    client.get("/v1/devices/dev-abc123/config")

    # Delete
    response = client.delete("/admin/devices/dev-abc123")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    # Verify deleted (should auto-register again)
    config = client.get("/v1/devices/dev-abc123/config")
    assert config.json()["is_assigned"] is False


def test_list_devices(client):
    """Test listing all devices."""
    # Register a few devices
    client.get("/v1/devices/dev-abc123/config")
    client.get("/v1/devices/dev-def456/config")

    # List devices
    response = client.get("/admin/devices?format=json")

    assert response.status_code == 200
    data = response.json()
    assert len(data["devices"]) == 2
    assert any(d["device_id"] == "dev-abc123" for d in data["devices"])
    assert any(d["device_id"] == "dev-def456" for d in data["devices"])


def test_get_device_config_after_assignment(client):
    """Test that device config returns assignment after assignment."""
    # Create rink and assign device
    client.post("/admin/rinks", json={
        "rink_id": "rink-alpha",
        "name": "Alpha Arena"
    })
    client.get("/v1/devices/dev-abc123/config")
    client.put("/admin/devices/dev-abc123", json={
        "rink_id": "rink-alpha",
        "sheet_name": "Sheet 1"
    })

    # Get config
    response = client.get("/v1/devices/dev-abc123/config")

    assert response.status_code == 200
    data = response.json()
    assert data["is_assigned"] is True
    assert data["rink_id"] == "rink-alpha"
    assert data["sheet_name"] == "Sheet 1"


def test_partial_device_update(client):
    """Test updating only some device fields."""
    # Create rink and assign device
    client.post("/admin/rinks", json={
        "rink_id": "rink-alpha",
        "name": "Alpha Arena"
    })
    client.get("/v1/devices/dev-abc123/config")
    client.put("/admin/devices/dev-abc123", json={
        "rink_id": "rink-alpha",
        "sheet_name": "Sheet 1",
        "notes": "Original notes"
    })

    # Update only notes
    response = client.put("/admin/devices/dev-abc123", json={
        "notes": "Updated notes"
    })

    assert response.status_code == 200
    data = response.json()
    assert data["device"]["notes"] == "Updated notes"
    assert data["device"]["sheet_name"] == "Sheet 1"  # Unchanged


def test_device_last_seen_updates(client):
    """Test that last_seen_at updates on config requests."""
    # First request
    response1 = client.get("/v1/devices/dev-abc123/config")
    first_seen = response1.json()

    # Wait a bit
    time.sleep(0.1)

    # Second request
    client.get("/v1/devices/dev-abc123/config")

    # Check via device list
    response = client.get("/admin/devices?format=json")
    devices = response.json()["devices"]
    device = next(d for d in devices if d["device_id"] == "dev-abc123")

    assert device["last_seen_at"] >= device["first_seen_at"]


def test_get_schedule_for_rink(client):
    """Test fetching schedule for a rink."""
    from datetime import datetime, timezone

    # Create rink
    client.post("/admin/rinks", json={
        "rink_id": "rink-alpha",
        "name": "Alpha Arena"
    })

    # Add games (note: would need to add via database or endpoint)
    # For now, test empty schedule
    response = client.get("/v1/rinks/rink-alpha/schedule")

    assert response.status_code == 200
    data = response.json()
    assert "games" in data
    assert "schedule_version" in data


def test_get_schedule_for_nonexistent_rink(client):
    """Test that getting schedule for non-existent rink fails."""
    response = client.get("/v1/rinks/rink-nonexistent/schedule")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_update_device_without_registering_fails(client):
    """Test that updating non-existent device fails."""
    response = client.put("/admin/devices/dev-nonexistent", json={
        "notes": "Some notes"
    })

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_assists_leaderboard(client, temp_db):
    """Test that assists are correctly aggregated in stats page."""
    import json

    # Set up test data
    conn = sqlite3.connect(temp_db)
    current_time = int(time.time())

    # Create league, season, division
    conn.execute("""
        INSERT INTO leagues (league_id, name, created_at)
        VALUES ('league-1', 'Test League', ?)
    """, (current_time,))

    conn.execute("""
        INSERT INTO seasons (season_id, name, start_date, created_at)
        VALUES ('season-1', '2025-2026', '2025-09-01', ?)
    """, (current_time,))

    conn.execute("""
        INSERT INTO divisions (division_id, name, created_at)
        VALUES ('div-1', 'Division A', ?)
    """, (current_time,))

    # Create teams
    conn.execute("""
        INSERT INTO teams (team_id, name, city, abbreviation, created_at)
        VALUES
            ('team-bruins', 'Bruins', 'Boston', 'BOS', ?),
            ('team-habs', 'Canadiens', 'Montreal', 'MTL', ?)
    """, (current_time, current_time))

    # Create team registrations
    conn.execute("""
        INSERT INTO team_registrations (registration_id, team_id, league_id, season_id, division_id, registered_at)
        VALUES ('reg-home', 'team-bruins', 'league-1', 'season-1', 'div-1', ?)
    """, (current_time,))

    conn.execute("""
        INSERT INTO team_registrations (registration_id, team_id, league_id, season_id, division_id, registered_at)
        VALUES ('reg-away', 'team-habs', 'league-1', 'season-1', 'div-1', ?)
    """, (current_time,))

    # Create rink and game
    conn.execute("""
        INSERT INTO rinks (rink_id, name, created_at)
        VALUES ('rink-1', 'Test Arena', ?)
    """, (current_time,))

    conn.execute("""
        INSERT INTO games (game_id, rink_id, home_team, away_team, home_registration_id, away_registration_id, start_time, period_length_min, created_at)
        VALUES ('game-1', 'rink-1', 'Bruins', 'Canadiens', 'reg-home', 'reg-away', '2025-09-15T19:00:00Z', 20, ?)
    """, (current_time,))

    # Create players
    conn.execute("""
        INSERT INTO players (player_id, full_name, first_name, last_name, jersey_number, created_at)
        VALUES
            (8471214, 'Brad Marchand', 'Brad', 'Marchand', 63, ?),
            (8474564, 'David Pastrnak', 'David', 'Pastrnak', 88, ?),
            (8475791, 'Charlie McAvoy', 'Charlie', 'McAvoy', 73, ?),
            (8470638, 'Patrice Bergeron', 'Patrice', 'Bergeron', 37, ?)
    """, (current_time, current_time, current_time, current_time))

    # Create goal events with assists
    # Goal 1: Marchand scores, Pastrnak primary assist, McAvoy secondary assist
    conn.execute("""
        INSERT INTO received_events (game_id, device_id, session_id, event_id, seq, type, ts_local, payload, received_at)
        VALUES ('game-1', 'dev-1', 'session-1', 'evt-1', 1, 'GOAL_HOME', '2025-09-15T19:15:00Z', ?, ?)
    """, (json.dumps({
        "goal_id": "goal-1",
        "value": 1,
        "time": "15:00",
        "scorer_id": "8471214",
        "assist1_id": "8474564",
        "assist2_id": "8475791"
    }), current_time))

    # Goal 2: Pastrnak scores, Marchand primary assist, Bergeron secondary assist
    conn.execute("""
        INSERT INTO received_events (game_id, device_id, session_id, event_id, seq, type, ts_local, payload, received_at)
        VALUES ('game-1', 'dev-1', 'session-1', 'evt-2', 2, 'GOAL_HOME', '2025-09-15T19:25:00Z', ?, ?)
    """, (json.dumps({
        "goal_id": "goal-2",
        "value": 1,
        "time": "10:00",
        "scorer_id": "8474564",
        "assist1_id": "8471214",
        "assist2_id": "8470638"
    }), current_time))

    # Goal 3: Bergeron scores, Pastrnak primary assist (unassisted on secondary)
    conn.execute("""
        INSERT INTO received_events (game_id, device_id, session_id, event_id, seq, type, ts_local, payload, received_at)
        VALUES ('game-1', 'dev-1', 'session-1', 'evt-3', 3, 'GOAL_HOME', '2025-09-15T19:35:00Z', ?, ?)
    """, (json.dumps({
        "goal_id": "goal-3",
        "value": 1,
        "time": "5:00",
        "scorer_id": "8470638",
        "assist1_id": "8474564",
        "assist2_id": None
    }), current_time))

    conn.commit()
    conn.close()

    # Query stats page with JSON format
    response = client.get("/admin/stats?format=json")

    assert response.status_code == 200
    data = response.json()

    # Check assists leaderboard
    assists = data["assists"]
    assert len(assists) > 0

    # Pastrnak should have 2 assists (1 primary on goal 1 + 1 primary on goal 3)
    pastrnak = next((p for p in assists if p["player_id"] == "8474564"), None)
    assert pastrnak is not None
    assert pastrnak["assists"] == 2
    assert pastrnak["full_name"] == "David Pastrnak"

    # Marchand should have 1 assist (1 primary on goal 2)
    marchand = next((p for p in assists if p["player_id"] == "8471214"), None)
    assert marchand is not None
    assert marchand["assists"] == 1
    assert marchand["full_name"] == "Brad Marchand"

    # McAvoy should have 1 assist (1 secondary on goal 1)
    mcavoy = next((p for p in assists if p["player_id"] == "8475791"), None)
    assert mcavoy is not None
    assert mcavoy["assists"] == 1
    assert mcavoy["full_name"] == "Charlie McAvoy"

    # Bergeron should have 1 assist (1 secondary on goal 2)
    bergeron = next((p for p in assists if p["player_id"] == "8470638"), None)
    assert bergeron is not None
    assert bergeron["assists"] == 1
    assert bergeron["full_name"] == "Patrice Bergeron"

    # Verify ordering (Pastrnak should be first with 2 assists)
    assert assists[0]["player_id"] == "8474564"


def test_stats_decrease_after_goal_cancellation(client, temp_db):
    """Test that stats properly decrease when a goal is cancelled."""
    import json

    # Set up test data
    conn = sqlite3.connect(temp_db)
    current_time = int(time.time())

    # Create league, season, division
    conn.execute("""
        INSERT INTO leagues (league_id, name, created_at)
        VALUES ('league-1', 'Test League', ?)
    """, (current_time,))

    conn.execute("""
        INSERT INTO seasons (season_id, name, start_date, created_at)
        VALUES ('season-1', '2025-2026', '2025-09-01', ?)
    """, (current_time,))

    conn.execute("""
        INSERT INTO divisions (division_id, name, created_at)
        VALUES ('div-1', 'Division A', ?)
    """, (current_time,))

    # Create teams
    conn.execute("""
        INSERT INTO teams (team_id, name, city, abbreviation, created_at)
        VALUES
            ('team-bruins', 'Bruins', 'Boston', 'BOS', ?),
            ('team-habs', 'Canadiens', 'Montreal', 'MTL', ?)
    """, (current_time, current_time))

    # Create team registrations
    conn.execute("""
        INSERT INTO team_registrations (registration_id, team_id, league_id, season_id, division_id, registered_at)
        VALUES ('reg-home', 'team-bruins', 'league-1', 'season-1', 'div-1', ?)
    """, (current_time,))

    # Create rink and game
    conn.execute("""
        INSERT INTO rinks (rink_id, name, created_at)
        VALUES ('rink-1', 'Test Arena', ?)
    """, (current_time,))

    conn.execute("""
        INSERT INTO games (game_id, rink_id, home_team, away_team, home_registration_id, away_registration_id, start_time, period_length_min, created_at)
        VALUES ('game-1', 'rink-1', 'Bruins', 'Canadiens', 'reg-home', 'reg-away', '2025-09-15T19:00:00Z', 20, ?)
    """, (current_time,))

    # Create players
    conn.execute("""
        INSERT INTO players (player_id, full_name, first_name, last_name, jersey_number, created_at)
        VALUES
            (8471214, 'Brad Marchand', 'Brad', 'Marchand', 63, ?),
            (8474564, 'David Pastrnak', 'David', 'Pastrnak', 88, ?)
    """, (current_time, current_time))

    # Goal 1: Marchand scores with Pastrnak assist (value=1)
    conn.execute("""
        INSERT INTO received_events (game_id, device_id, session_id, event_id, seq, type, ts_local, payload, received_at)
        VALUES ('game-1', 'dev-1', 'session-1', 'evt-1', 1, 'GOAL_HOME', '2025-09-15T19:15:00Z', ?, ?)
    """, (json.dumps({
        "goal_id": "goal-1",
        "value": 1,
        "time": "15:00",
        "scorer_id": "8471214",
        "assist1_id": "8474564",
        "assist2_id": None
    }), current_time))

    # Goal 2: Pastrnak scores with Marchand assist (value=1)
    conn.execute("""
        INSERT INTO received_events (game_id, device_id, session_id, event_id, seq, type, ts_local, payload, received_at)
        VALUES ('game-1', 'dev-1', 'session-1', 'evt-2', 2, 'GOAL_HOME', '2025-09-15T19:20:00Z', ?, ?)
    """, (json.dumps({
        "goal_id": "goal-2",
        "value": 1,
        "time": "10:00",
        "scorer_id": "8474564",
        "assist1_id": "8471214",
        "assist2_id": None
    }), current_time))

    # Cancel Goal 1 (value=-1 with same player IDs)
    conn.execute("""
        INSERT INTO received_events (game_id, device_id, session_id, event_id, seq, type, ts_local, payload, received_at)
        VALUES ('game-1', 'dev-1', 'session-1', 'evt-3', 3, 'GOAL_HOME', '2025-09-15T19:25:00Z', ?, ?)
    """, (json.dumps({
        "goal_id": "goal-1",
        "value": -1,
        "time": "15:00",
        "scorer_id": "8471214",
        "assist1_id": "8474564",
        "assist2_id": None
    }), current_time))

    conn.commit()
    conn.close()

    # Query stats
    response = client.get("/admin/stats?format=json")

    assert response.status_code == 200
    data = response.json()

    # Check goal stats
    scorers = data["scorers"]

    # Marchand should have 0 goals (1 goal scored - 1 cancelled = 0)
    # He shouldn't appear in the leaderboard (HAVING goals > 0)
    marchand = next((p for p in scorers if p["player_id"] == "8471214"), None)
    assert marchand is None, "Marchand should not appear with 0 goals"

    # Pastrnak should have 1 goal (1 goal scored)
    pastrnak = next((p for p in scorers if p["player_id"] == "8474564"), None)
    assert pastrnak is not None
    assert pastrnak["goals"] == 1

    # Check assist stats
    assists = data["assists"]

    # Pastrnak should have 0 assists (1 assist - 1 cancelled = 0)
    pastrnak_assists = next((p for p in assists if p["player_id"] == "8474564"), None)
    assert pastrnak_assists is None, "Pastrnak should not appear with 0 assists"

    # Marchand should have 1 assist (on goal-2)
    marchand_assists = next((p for p in assists if p["player_id"] == "8471214"), None)
    assert marchand_assists is not None
    assert marchand_assists["assists"] == 1


def test_points_leaderboard(client, temp_db):
    """Test that points (goals + assists) are correctly aggregated in stats page."""
    import json

    # Set up test data
    conn = sqlite3.connect(temp_db)
    current_time = int(time.time())

    # Create league, season, division
    conn.execute("""
        INSERT INTO leagues (league_id, name, created_at)
        VALUES ('league-1', 'Test League', ?)
    """, (current_time,))

    conn.execute("""
        INSERT INTO seasons (season_id, name, start_date, created_at)
        VALUES ('season-1', '2025-2026', '2025-09-01', ?)
    """, (current_time,))

    conn.execute("""
        INSERT INTO divisions (division_id, name, created_at)
        VALUES ('div-1', 'Division A', ?)
    """, (current_time,))

    # Create teams
    conn.execute("""
        INSERT INTO teams (team_id, name, city, abbreviation, created_at)
        VALUES
            ('team-bruins', 'Bruins', 'Boston', 'BOS', ?),
            ('team-habs', 'Canadiens', 'Montreal', 'MTL', ?)
    """, (current_time, current_time))

    # Create team registrations
    conn.execute("""
        INSERT INTO team_registrations (registration_id, team_id, league_id, season_id, division_id, registered_at)
        VALUES ('reg-home', 'team-bruins', 'league-1', 'season-1', 'div-1', ?)
    """, (current_time,))

    conn.execute("""
        INSERT INTO team_registrations (registration_id, team_id, league_id, season_id, division_id, registered_at)
        VALUES ('reg-away', 'team-habs', 'league-1', 'season-1', 'div-1', ?)
    """, (current_time,))

    # Create rink and game
    conn.execute("""
        INSERT INTO rinks (rink_id, name, created_at)
        VALUES ('rink-1', 'Test Arena', ?)
    """, (current_time,))

    conn.execute("""
        INSERT INTO games (game_id, rink_id, home_team, away_team, home_registration_id, away_registration_id, start_time, period_length_min, created_at)
        VALUES ('game-1', 'rink-1', 'Bruins', 'Canadiens', 'reg-home', 'reg-away', '2025-09-15T19:00:00Z', 20, ?)
    """, (current_time,))

    # Create players
    conn.execute("""
        INSERT INTO players (player_id, full_name, first_name, last_name, jersey_number, created_at)
        VALUES
            (8471214, 'Brad Marchand', 'Brad', 'Marchand', 63, ?),
            (8474564, 'David Pastrnak', 'David', 'Pastrnak', 88, ?),
            (8475791, 'Charlie McAvoy', 'Charlie', 'McAvoy', 73, ?)
    """, (current_time, current_time, current_time))

    # Add roster entries with jersey numbers
    conn.execute("""
        INSERT INTO roster_entries (registration_id, player_id, jersey_number, position, added_at)
        VALUES
            ('reg-home', 8471214, 63, 'LW', ?),
            ('reg-home', 8474564, 88, 'RW', ?),
            ('reg-home', 8475791, 73, 'D', ?)
    """, (current_time, current_time, current_time))

    # Create goal events with assists
    # Goal 1: Marchand scores (1G), Pastrnak primary assist (1A), McAvoy secondary assist (1A)
    conn.execute("""
        INSERT INTO received_events (game_id, device_id, session_id, event_id, seq, type, ts_local, payload, received_at)
        VALUES ('game-1', 'dev-1', 'session-1', 'evt-1', 1, 'GOAL_HOME', '2025-09-15T19:15:00Z', ?, ?)
    """, (json.dumps({
        "goal_id": "goal-1",
        "value": 1,
        "time": "15:00",
        "scorer_id": "8471214",
        "assist1_id": "8474564",
        "assist2_id": "8475791"
    }), current_time))

    # Goal 2: Pastrnak scores (1G), Marchand primary assist (1A)
    conn.execute("""
        INSERT INTO received_events (game_id, device_id, session_id, event_id, seq, type, ts_local, payload, received_at)
        VALUES ('game-1', 'dev-1', 'session-1', 'evt-2', 2, 'GOAL_HOME', '2025-09-15T19:25:00Z', ?, ?)
    """, (json.dumps({
        "goal_id": "goal-2",
        "value": 1,
        "time": "10:00",
        "scorer_id": "8474564",
        "assist1_id": "8471214",
        "assist2_id": None
    }), current_time))

    # Goal 3: Marchand scores (1G), Pastrnak primary assist (1A), McAvoy secondary assist (1A)
    conn.execute("""
        INSERT INTO received_events (game_id, device_id, session_id, event_id, seq, type, ts_local, payload, received_at)
        VALUES ('game-1', 'dev-1', 'session-1', 'evt-3', 3, 'GOAL_HOME', '2025-09-15T19:35:00Z', ?, ?)
    """, (json.dumps({
        "goal_id": "goal-3",
        "value": 1,
        "time": "5:00",
        "scorer_id": "8471214",
        "assist1_id": "8474564",
        "assist2_id": "8475791"
    }), current_time))

    conn.commit()
    conn.close()

    # Query stats page with JSON format
    response = client.get("/admin/stats?format=json")

    assert response.status_code == 200
    data = response.json()

    # Check points leaderboard
    points = data["points"]
    assert len(points) > 0

    # Marchand should have 3 points (2 goals + 1 assist) - ranked first due to more goals
    marchand = next((p for p in points if p["player_id"] == "8471214"), None)
    assert marchand is not None
    assert marchand["points"] == 3
    assert marchand["goals"] == 2
    assert marchand["assists"] == 1
    assert marchand["full_name"] == "Brad Marchand"
    assert marchand["team_abbrev"] == "BOS"
    assert marchand["jersey_number"] == 63
    assert marchand["league_name"] == "Test League"
    assert marchand["season_name"] == "2025-2026"
    assert marchand["division_name"] == "Division A"

    # Pastrnak should have 3 points (1 goal + 2 assists) - ranked second (fewer goals than Marchand)
    pastrnak = next((p for p in points if p["player_id"] == "8474564"), None)
    assert pastrnak is not None
    assert pastrnak["points"] == 3
    assert pastrnak["goals"] == 1
    assert pastrnak["assists"] == 2
    assert pastrnak["full_name"] == "David Pastrnak"
    assert pastrnak["team_abbrev"] == "BOS"
    assert pastrnak["jersey_number"] == 88
    assert pastrnak["league_name"] == "Test League"
    assert pastrnak["season_name"] == "2025-2026"
    assert pastrnak["division_name"] == "Division A"

    # McAvoy should have 2 points (0 goals + 2 assists)
    mcavoy = next((p for p in points if p["player_id"] == "8475791"), None)
    assert mcavoy is not None
    assert mcavoy["points"] == 2
    assert mcavoy["goals"] == 0
    assert mcavoy["assists"] == 2
    assert mcavoy["full_name"] == "Charlie McAvoy"
    assert mcavoy["team_abbrev"] == "BOS"
    assert mcavoy["jersey_number"] == 73
    assert mcavoy["league_name"] == "Test League"
    assert mcavoy["season_name"] == "2025-2026"
    assert mcavoy["division_name"] == "Division A"

    # Verify ordering - ties broken by goals (Marchand 2G > Pastrnak 1G)
    assert points[0]["player_id"] == "8471214"  # Marchand first (3pts, 2G)
    assert points[1]["player_id"] == "8474564"  # Pastrnak second (3pts, 1G)



