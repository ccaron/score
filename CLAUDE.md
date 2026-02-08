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

### Schedule Generation
```bash
# Generate a schedule from YAML config
make schedule CONFIG=examples/schedule.yaml

# Or directly
uv run score-schedule examples/schedule.yaml
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
- `src/score/db.py` - Local database utilities and initialization

### Cloud API
- `src/score/cloud.py` - Cloud API simulator with schedule management, event reception, device management
- `src/score/schema.py` - Cloud database schema definitions (all table DDL, indexes, default data)
- `src/score/seed.py` - Database seeding functions for development/testing
- Database: `cloud.db`

### Models & Configuration
- `src/score/models.py` - Shared Pydantic models for API requests/responses
- `src/score/config.py` - Database paths, cloud API URL
- `src/score/device.py` - Device ID generation from MAC address
- `src/score/log.py` - Queue-based logging setup

### Schedule Generation
- `src/score/scheduler.py` - Schedule generation using OR-Tools CP-SAT solver
- `examples/schedule.yaml` - Example schedule configuration

### Testing
Tests use pytest and are organized by feature:
- `tests/test_cli.py` - Main app functionality
- `tests/test_goals.py` - Goal scoring, cancellation, and attribution
- `tests/test_state.py` - Event replay logic
- `tests/test_replay_determinism.py` - Event replay determinism verification
- `tests/test_event_pusher.py` - Event delivery
- `tests/test_pusher_errors.py` - Pusher error handling
- `tests/test_cloud_admin.py` - Cloud API admin endpoints
- `tests/test_schedule.py` - Schedule download
- `tests/test_scheduler.py` - Schedule generation
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

The cloud database schema is defined in `src/score/schema.py`. Key entity groups:

**Permanent Entities:**
- `leagues` - Organizations that run competitions
- `seasons` - Time periods for competitions
- `divisions` - Team groupings (supports nesting via `parent_division_id`)
- `tournaments` - Time-bound events (alternative to league+season)
- `teams` - Team organizations
- `players` - Individual athletes
- `rinks` - Physical venues
- `rink_sheets` - Ice surfaces within a rink
- `officials` - Referees and linesmen

**Rule Configuration:**
- `rule_sets` - League-specific rules (period length, checking rules, point systems)
- `rule_set_infractions` - Penalty definitions per rule set

**Temporal Participation:**
- `league_seasons` - League operates during a season
- `team_registrations` - Team competing in a context (THE ROSTER anchor)
- `roster_entries` - Player on a team's roster with temporal tracking (`added_at`/`removed_at`)
- `spare_players` - Players available to sub

**Games & Events:**
- `games` - Scheduled games with venue, teams, timing
- `events` - Append-only event log per game
- `game_officials` - Officials assigned to games

**Device Management:**
- `devices` - Registered scoreboard devices
- `heartbeats` - Device status updates
- `received_events` - Events uploaded from devices
- `schedule_versions` - Version tracking for schedule sync

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
    "home_shots": 0,
    "away_shots": 0,
    "home_roster": [],      # Active player IDs
    "away_roster": [],
    "roster_details": {},   # player_id -> player info dict
    "period": 1,            # Current period number
    "penalties": [],        # List of active penalties
    "home_goalie_id": None, # Current home goalie
    "away_goalie_id": None, # Current away goalie
    "faceoffs": {"home": 0, "away": 0},
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

### Supported Event Types

**Clock/Game Flow:**
- `CLOCK_SET` - Set clock time
- `CLOCK_START`, `CLOCK_STOP` - Start/stop clock
- `GAME_STARTED`, `GAME_PAUSED`, `GAME_END` - Game state changes
- `PERIOD_START`, `PERIOD_END` - Period transitions

**Scoring:**
- `GOAL_HOME`, `GOAL_AWAY` - Goals with value (+1 for goal, -1 for cancellation)
- `SHOT_HOME`, `SHOT_AWAY` - Shot tracking

**Penalties:**
- `PENALTY` - Penalty assessed
- `PENALTY_START`, `PENALTY_END` - Penalty clock management

**Other:**
- `GOALIE_IN`, `GOALIE_OUT` - Goalie changes
- `FACEOFF` - Faceoff wins
- `ROSTER_INITIALIZED`, `ROSTER_PLAYER_SCRATCHED`, `ROSTER_PLAYER_ACTIVATED` - Roster management

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

### Working with the Schema

The cloud database schema is centralized in `src/score/schema.py`:
- All table DDL is in the `TABLES` constant
- Indexes are in the `INDEXES` constant
- Default rule sets and infractions are seeded automatically
- Use `init_schema(db_path, fresh_start=True)` to reset the database

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

## Schedule Generation

The `src/score/scheduler.py` module uses Google OR-Tools CP-SAT solver to generate fair hockey schedules.

### Configuration (YAML)

```yaml
league_id: "baal"
season_id: "2025-2026"
rink_id: "sharks-ice"

sheets:
  - sheet_id: "sharks-ice-a"
    name: "Sheet A"

divisions:
  - division_id: "div-a"
    games_per_team: 12
    teams:
      - registration_id: "reg-dogs-2025"
        name: "Ice Dogs"
        abbreviation: "DOG"

schedule:
  days_of_week: ["sunday"]
  start_date: "2025-01-05"
  end_date: "2025-04-27"
  blackout_dates: ["2025-02-16"]
  time_slots: ["18:00", "19:30", "21:00"]

game_settings:
  period_length_min: 15
  num_periods: 3
  game_type: "regular"

solver:
  timeout_seconds: 60
  weight_time_slot: 10    # Balance games across time slots
  weight_sheet: 10        # Balance games across sheets
  weight_home_away: 20    # Balance home/away games
  weight_opponent: 5      # Spread games across opponents
```

### Fairness Constraints

The solver optimizes for:
- **Time slot balance**: Each team plays roughly equal games at each time
- **Sheet balance**: Each team plays roughly equal games on each sheet
- **Home/away balance**: Each team has roughly equal home and away games
- **Opponent variety**: Games spread across opponents evenly
- **No consecutive opponents**: Penalizes playing same opponent in back-to-back weeks
- **Max consecutive byes**: Hard constraint on weeks without games
