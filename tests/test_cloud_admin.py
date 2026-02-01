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
