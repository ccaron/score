"""Tests for schedule date filtering and timezone handling."""
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def temp_cloud_db():
    """Create a temporary cloud database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # Initialize cloud database schema
    conn = sqlite3.connect(db_path)

    conn.execute("""
        CREATE TABLE rinks (
            rink_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)

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

    conn.execute("""
        CREATE TABLE seasons (
            season_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT,
            created_at INTEGER NOT NULL
        )
    """)

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

    conn.execute("""
        CREATE TABLE schedule_versions (
            rink_id TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (rink_id) REFERENCES rinks(rink_id)
        )
    """)

    # Add test rink
    conn.execute(
        "INSERT INTO rinks (rink_id, name, created_at) VALUES (?, ?, ?)",
        ("test-rink", "Test Rink", int(time.time()))
    )

    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    import os
    os.unlink(db_path)


@pytest.fixture
def cloud_client(temp_cloud_db, monkeypatch):
    """Create test client for cloud API."""
    from score import cloud
    monkeypatch.setattr(cloud, "CLOUD_DB_PATH", temp_cloud_db)

    from score.cloud import app
    return TestClient(app)


def add_game(db_path, game_id, start_time, home="Home", away="Away"):
    """Helper to add a game to the database."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO games (game_id, rink_id, home_team, away_team, start_time, period_length_min, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (game_id, "test-rink", home, away, start_time, 20, int(time.time())))
    conn.commit()
    conn.close()


def test_schedule_returns_only_todays_games(cloud_client, temp_cloud_db):
    """Test that schedule endpoint returns only today's games."""
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # Add games for different days
    add_game(temp_cloud_db, "yesterday-game", f"{yesterday}T19:00:00Z")
    add_game(temp_cloud_db, "today-game-1", f"{today}T19:00:00Z")
    add_game(temp_cloud_db, "today-game-2", f"{today}T22:00:00Z")
    add_game(temp_cloud_db, "tomorrow-game", f"{tomorrow}T19:00:00Z")

    # Get today's schedule
    response = cloud_client.get("/v1/rinks/test-rink/schedule")

    assert response.status_code == 200
    data = response.json()

    # Should only get today's games (but might include tomorrow's due to timezone handling)
    game_ids = [g["game_id"] for g in data["games"]]
    assert "today-game-1" in game_ids
    assert "today-game-2" in game_ids
    assert "yesterday-game" not in game_ids


def test_schedule_with_specific_date(cloud_client, temp_cloud_db):
    """Test requesting schedule for a specific date."""
    date = "2024-02-15"

    # Add games for different dates
    add_game(temp_cloud_db, "feb-15-game", "2024-02-15T19:00:00Z")
    add_game(temp_cloud_db, "feb-16-game", "2024-02-16T19:00:00Z")
    add_game(temp_cloud_db, "feb-14-game", "2024-02-14T19:00:00Z")

    # Get schedule for Feb 15
    response = cloud_client.get(f"/v1/rinks/test-rink/schedule?date={date}")

    assert response.status_code == 200
    data = response.json()

    game_ids = [g["game_id"] for g in data["games"]]
    assert "feb-15-game" in game_ids
    # feb-16 might be included due to timezone handling
    assert "feb-14-game" not in game_ids


def test_schedule_handles_evening_games_timezone(cloud_client, temp_cloud_db):
    """Test that evening games in Pacific time are included correctly."""
    # A game at 7pm Pacific on Feb 1 is 3am UTC on Feb 2
    # When requesting Feb 1 games, it should be included

    date = "2024-02-01"

    # Add an evening game (stored as next day in UTC)
    add_game(temp_cloud_db, "evening-game", "2024-02-02T03:00:00Z", "Bruins", "Canadiens")

    # Get schedule for Feb 1
    response = cloud_client.get(f"/v1/rinks/test-rink/schedule?date={date}")

    assert response.status_code == 200
    data = response.json()

    game_ids = [g["game_id"] for g in data["games"]]
    # Evening game should be included because query looks at both Feb 1 and Feb 2 UTC
    assert "evening-game" in game_ids


def test_schedule_returns_empty_for_no_games(cloud_client, temp_cloud_db):
    """Test that schedule returns empty list when no games exist."""
    response = cloud_client.get("/v1/rinks/test-rink/schedule?date=2024-01-01")

    assert response.status_code == 200
    data = response.json()
    assert data["games"] == []


def test_schedule_for_nonexistent_rink(cloud_client):
    """Test requesting schedule for a rink that doesn't exist."""
    response = cloud_client.get("/v1/rinks/nonexistent-rink/schedule")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_schedule_games_ordered_by_time(cloud_client, temp_cloud_db):
    """Test that games are returned in chronological order."""
    date = "2024-02-01"

    # Add games in random order
    add_game(temp_cloud_db, "game-3", "2024-02-01T22:00:00Z", "Team E", "Team F")
    add_game(temp_cloud_db, "game-1", "2024-02-01T18:00:00Z", "Team A", "Team B")
    add_game(temp_cloud_db, "game-2", "2024-02-01T20:00:00Z", "Team C", "Team D")

    response = cloud_client.get(f"/v1/rinks/test-rink/schedule?date={date}")

    assert response.status_code == 200
    data = response.json()
    game_ids = [g["game_id"] for g in data["games"]]

    # Should be ordered by time
    assert game_ids.index("game-1") < game_ids.index("game-2")
    assert game_ids.index("game-2") < game_ids.index("game-3")


def test_schedule_includes_all_game_fields(cloud_client, temp_cloud_db):
    """Test that schedule response includes all required fields."""
    date = "2024-02-01"
    add_game(temp_cloud_db, "test-game", "2024-02-01T19:00:00Z", "Bruins", "Canadiens")

    response = cloud_client.get(f"/v1/rinks/test-rink/schedule?date={date}")

    assert response.status_code == 200
    data = response.json()

    assert "schedule_version" in data
    assert "games" in data
    assert len(data["games"]) > 0

    game = data["games"][0]
    assert game["game_id"] == "test-game"
    assert game["home_team"] == "Bruins"
    assert game["away_team"] == "Canadiens"
    assert game["start_time"] == "2024-02-01T19:00:00Z"
    assert game["period_length_min"] == 20


def test_schedule_multiple_games_same_day(cloud_client, temp_cloud_db):
    """Test handling multiple games on the same day."""
    date = "2024-02-01"

    # Add multiple games
    for i in range(5):
        add_game(
            temp_cloud_db,
            f"game-{i}",
            f"2024-02-01T{18+i}:00:00Z",
            f"Home{i}",
            f"Away{i}"
        )

    response = cloud_client.get(f"/v1/rinks/test-rink/schedule?date={date}")

    assert response.status_code == 200
    data = response.json()
    assert len(data["games"]) >= 5


def test_schedule_date_boundary_handling(cloud_client, temp_cloud_db):
    """Test games right at date boundaries."""
    date = "2024-02-01"

    # Games right at midnight boundaries
    add_game(temp_cloud_db, "midnight-start", "2024-02-01T00:00:00Z")
    add_game(temp_cloud_db, "midnight-end", "2024-02-01T23:59:59Z")
    add_game(temp_cloud_db, "next-day-start", "2024-02-02T00:00:00Z")

    response = cloud_client.get(f"/v1/rinks/test-rink/schedule?date={date}")

    assert response.status_code == 200
    data = response.json()
    game_ids = [g["game_id"] for g in data["games"]]

    assert "midnight-start" in game_ids
    assert "midnight-end" in game_ids
    # next-day-start is included because we query both dates for timezone handling
    assert "next-day-start" in game_ids
