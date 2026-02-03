# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Score is a multi-component game clock/scoreboard system with event sourcing architecture. It consists of:

1. **score-app** - Game clock application with web UI (port 8000)
2. **score-cloud** - Cloud API simulator for schedule downloads and event uploads (port 8001)
3. **Event Pusher** - Background process that delivers events to destinations

The system is designed for hockey rinks where mini PCs run score-app displays, sync with a cloud backend for schedules, and push game events for monitoring/analytics.

## Commands

### Development
```bash
# Install dependencies and run both apps (score-app on :8000, score-cloud on :8001)
make run

# Run tests
make test

# Run single test file
uv run pytest tests/test_goals.py

# Run specific test
uv run pytest tests/test_goals.py::test_goal_cancel

# Run just score-app
uv run score-app

# Run just score-cloud
uv run score-cloud

# Run event pusher standalone (for debugging)
uv run score-push-events
```

### Docker
```bash
make run_container
```

### Database Inspection
```bash
# View local game database
sqlite3 game.db "SELECT * FROM events;"

# View cloud database
sqlite3 cloud.db "SELECT * FROM received_events;"
```

## Architecture Fundamentals

### Multi-Process Design

The application uses **separate processes** for isolation:
- **Main Process**: FastAPI server, WebSocket, game loop, UI
- **Event Pusher Process**: Polls database, delivers events to destinations (file, cloud API)
- **Communication**: Shared SQLite database, process health monitoring via `is_alive()`

This prevents I/O operations from blocking the game clock and allows independent process crashes.

### Event Sourcing

State is reconstructed by **replaying events**:
- Events stored in SQLite with timestamps
- State replay logic in `src/score/state.py` shared between score-app and score-cloud
- On app start, all events are replayed to restore current state
- Supports multiple event types: `CLOCK_SET`, `GAME_STARTED`, `GAME_PAUSED`, `GOAL_HOME`, `GOAL_AWAY`, `SHOT_HOME`, `SHOT_AWAY`, `ROSTER_INITIALIZED`, `ROSTER_PLAYER_SCRATCHED`, `ROSTER_PLAYER_ACTIVATED`

### Delivery System

Events are delivered to destinations with tracking:
- `events` table: Stores all events
- `deliveries` table: Tracks delivery status per destination (NULL/1/2 = pending/success/failure)
- Event pusher polls every 0.5s for undelivered events
- Supports delivery to cloud API (`http://localhost:8001`)

### Logging Coordination

Uses **queue-based logging** to coordinate output from multiple processes:
- Main process logs directly to Rich console handler
- Pusher process sends log records to multiprocessing.Queue
- Queue listener in main process forwards to Rich handler
- Format: `[HH:MM:SS] [PID: 12345 TID: 67890] Message`

## Key Files

### Core Application
- `src/score/app.py` - Main FastAPI app, WebSocket server, game loop, HTML UI
- `src/score/state.py` - **Shared** event replay logic (used by both app and cloud)
- `src/score/pusher.py` - Event delivery process (cloud pusher)

### Cloud API
- `src/score/cloud.py` - Cloud API simulator with schedule management, event reception, device management
- Database: `cloud.db` (rinks, games, devices, received_events, heartbeats)

### Configuration & Utilities
- `src/score/config.py` - Database paths, cloud API URL
- `src/score/device.py` - Device ID generation from MAC address
- `src/score/log.py` - Queue-based logging setup

### Testing
Tests use pytest and are organized by feature:
- `tests/test_cli.py` - Main app functionality
- `tests/test_goals.py` - Goal scoring, cancellation, and attribution
- `tests/test_state.py` - Event replay logic
- `tests/test_event_pusher.py` - Event delivery
- `tests/test_pusher_errors.py` - Pusher error handling
- `tests/test_cloud_admin.py` - Cloud API admin endpoints
- `tests/test_schedule.py` - Schedule download
- `tests/test_multi_game.py` - Multi-game scenarios
- `tests/test_log.py` - Logging infrastructure

## Database Schemas

### Local Game Database (`game.db`)

```sql
-- Event log (source of truth)
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    game_id TEXT,           -- Associated game
    payload TEXT,           -- JSON string
    created_at INTEGER NOT NULL
);

-- Delivery tracking
CREATE TABLE deliveries (
    event_id INTEGER,
    destination TEXT,
    delivered INTEGER,      -- 0=pending, 1=success, 2=failed
    delivered_at INTEGER,
    PRIMARY KEY (event_id, destination)
);
```

### Cloud Database (`cloud.db`)

```sql
-- Rinks/venues
CREATE TABLE rinks (
    rink_id TEXT PRIMARY KEY,
    name TEXT
);

-- Devices (mini PCs)
CREATE TABLE devices (
    device_id TEXT PRIMARY KEY,  -- Generated from MAC address
    rink_id TEXT,
    sheet_name TEXT,
    device_name TEXT,
    is_assigned INTEGER,
    first_seen_at INTEGER,
    last_seen_at INTEGER
);

-- Game schedules
CREATE TABLE games (
    game_id TEXT PRIMARY KEY,
    rink_id TEXT,
    home_team TEXT,
    away_team TEXT,
    home_abbrev TEXT,           -- Team abbreviation (e.g., "TOR")
    away_abbrev TEXT,
    start_time TEXT,
    period_length_min INTEGER
);

-- Events uploaded from devices
CREATE TABLE received_events (
    id INTEGER PRIMARY KEY,
    game_id TEXT,
    device_id TEXT,
    event_id TEXT UNIQUE,       -- Idempotency key
    seq INTEGER,
    type TEXT,
    payload TEXT,
    received_at INTEGER
);

-- Device heartbeats
CREATE TABLE heartbeats (
    device_id TEXT,
    ts_local TEXT,
    current_game_id TEXT,
    game_state TEXT,
    received_at INTEGER
);

-- Schedule version tracking
CREATE TABLE schedule_versions (
    rink_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Players (master data from NHL API)
CREATE TABLE players (
    player_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    jersey_number INTEGER,
    position TEXT,              -- C, LW, RW, D, G
    shoots_catches TEXT,        -- L, R
    height_inches INTEGER,
    weight_pounds INTEGER,
    birth_date TEXT,
    birth_city TEXT,
    birth_country TEXT,
    created_at INTEGER NOT NULL
);

-- Teams (master data)
CREATE TABLE teams (
    team_abbrev TEXT PRIMARY KEY,  -- e.g., "TOR", "MTL"
    city TEXT NOT NULL,
    team_name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    conference TEXT,
    division TEXT,
    created_at INTEGER NOT NULL
);

-- Team rosters (temporal tracking)
CREATE TABLE team_rosters (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL,
    team_abbrev TEXT NOT NULL,
    roster_status TEXT NOT NULL,   -- Y (active), I (injured), etc.
    added_at INTEGER NOT NULL,     -- When player joined team
    removed_at INTEGER,            -- When player left (NULL if current)
    UNIQUE(player_id, team_abbrev, added_at)
);
```

## State Management Details

The `replay_events()` function in `src/score/state.py` is **central** to the system:

```python
state = {
    "seconds": 0,           # Time remaining
    "running": False,       # Clock running?
    "last_update": timestamp,
    "home_score": 0,
    "away_score": 0,
    "goals": [],            # Goal history (see structure below)
    "home_shots": 0,        # Shot count
    "away_shots": 0,
    "home_roster": [],      # Active player IDs
    "away_roster": [],
    "roster_details": {}    # player_id -> player info dict
}

# Goal structure includes attribution:
goal = {
    "id": "goal_uuid",
    "team": "home" | "away",
    "time": "12:34",        # Clock time when scored
    "cancelled": False,
    "scorer_id": player_id,  # Optional
    "assist1_id": player_id, # Optional
    "assist2_id": player_id  # Optional
}
```

Events are replayed chronologically to compute current state. When clock is running, elapsed time is calculated from `last_update` to current time.

## Cloud API Endpoints

Base URL: `http://localhost:8001`

### Main API
- `GET /v1/rinks/{rink_id}/schedule?date=YYYY-MM-DD` - Download schedule
- `POST /v1/games/{game_id}/events` - Upload events (idempotent via `event_id`)
- `POST /v1/heartbeat` - Device status updates

### Admin/Debug
- `GET /admin/heartbeats/latest` - View latest heartbeats per device
- `GET /admin/events/{game_id}` - View events for game
- `GET /admin/games/state` - Rendered HTML page showing all game states
- `GET /admin/devices` - List all registered devices
- `PUT /admin/devices/{device_id}` - Assign device to rink/sheet
- `DELETE /admin/devices/{device_id}/assignment` - Unassign device

See `CLOUD_API.md` and `DEVICE_MANAGEMENT.md` for detailed API documentation.

## Development Patterns

### Adding New Event Types

1. Add event type constant (e.g., `"PENALTY"`)
2. Create database event in appropriate API endpoint
3. Update `replay_events()` in `src/score/state.py` to handle new type
4. Add test in `tests/test_state.py`
5. Update cloud database if needed

### Adding New Pusher Destinations

1. Create new pusher class inheriting base pattern from `pusher.py`
2. Implement delivery logic
3. Add destination to `DESTINATIONS` list in app startup
4. Spawn separate process if needed
5. Add status tracking to game loop

### Working with Tests

- Tests use temporary databases (cleaned up automatically)
- FastAPI TestClient for API testing
- Tests verify event replay logic produces correct state
- Use pytest fixtures for common setup (see existing tests for patterns)

## Important Considerations

### Event Replay Must Be Deterministic
When modifying `replay_events()`, ensure replaying the same events always produces the same state. Avoid non-deterministic operations (random, current time without parameters).

### Database Locking
SQLite has 5s lock timeout. Event pusher polls every 0.5s. Be mindful of long-running transactions blocking the pusher.

### Process Health Monitoring
The game loop checks pusher process health every second using `process.is_alive()`. Status indicator shows:
- **Green**: Process alive, no pending events
- **Yellow**: Process alive, pending events
- **Red**: Process dead

### Idempotency
Cloud API event upload is idempotent via `event_id` UNIQUE constraint. Duplicate events are silently ignored. This allows safe retries on network failures.

### WebSocket Broadcasting
Game state is broadcast to all connected WebSocket clients every 1 second. Keep broadcast payloads small for performance.

## Common Debugging Steps

1. **Check database contents**: Use sqlite3 CLI to inspect `game.db` or `cloud.db`
2. **Check process status**: Game loop logs pusher health every second
3. **Inspect cloud state**: Visit `http://localhost:8001/admin/games/state` to see reconstructed game state
4. **Test event replay**: Unit tests in `tests/test_state.py` verify replay logic
5. **Monitor logs**: Rich console output shows PID/TID for multi-process coordination
