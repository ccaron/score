import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from main import app, state, get_db, init_db
import sqlite3

# Use a fresh in-memory SQLite for tests
@pytest.fixture(autouse=True)
def override_db(monkeypatch):
    def _get_db():
        conn = sqlite3.connect(":memory:")  # in-memory DB
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr("main.get_db", _get_db)
    init_db()

client = TestClient(app)

@pytest.mark.asyncio
async def test_initial_state():
    # Should start at 20:00
    r = client.get("/")
    assert r.status_code == 200
    assert state.seconds == 20 * 60
    assert state.running == False

@pytest.mark.asyncio
async def test_start_pause_set_time():
    # Start game
    r = client.post("/start")
    assert r.status_code == 200
    assert state.running == True

    # Pause game
    r = client.post("/pause")
    assert r.status_code == 200
    assert state.running == False

    # Set clock to 5:30
    r = client.post("/set_time", params={"time_str": "5:30"})
    assert r.status_code == 200
    assert state.seconds == 5*60 + 30

@pytest.mark.asyncio
async def test_clock_tick():
    state.seconds = 10
    state.running = True

    # Run one tick
    async def tick_once():
        if state.running and state.seconds > 0:
            state.seconds -= 1

    await tick_once()
    assert state.seconds == 9

@pytest.mark.asyncio
async def test_debug_events():
    # Add some events
    state.add_event("GAME_STARTED")
    state.add_event("CLOCK_SET", {"seconds": 123})

    r = client.post("/debug_events")
    assert r.status_code == 200

    # DB should have 2 events
    db = get_db()
    rows = db.execute("SELECT * FROM events").fetchall()
    db.close()
    assert len(rows) == 2
    assert rows[0]["type"] == "GAME_STARTED"
    assert json.loads(rows[1]["payload"])["seconds"] == 123
