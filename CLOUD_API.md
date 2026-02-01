# Cloud API Simulator

This is a simulated cloud backend for the scoreboard system. Mini PCs connect to this API to download schedules, upload events, and send heartbeats.

## Quick Start

### Running the Cloud API

```bash
# Option 1: Run directly with Python
python -m score.cloud

# Option 2: Using the installed command
uv run score-cloud

# Option 3: Using the Makefile (runs both apps)
make run
```

The API will start on **http://localhost:8001**

The main scoreboard app runs on port 8000, so this cloud API uses port 8001 to avoid conflicts.

## API Endpoints

### 1. Download Game Schedule

**GET** `/v1/rinks/{rink_id}/schedule?date=YYYY-MM-DD`

Download the game schedule for a specific rink.

**Parameters:**
- `rink_id` (path) - Rink identifier
- `date` (query, optional) - Date in YYYY-MM-DD format, defaults to today

**Example Request:**
```bash
curl "http://localhost:8001/v1/rinks/rink-alpha/schedule?date=2026-02-01"
```

**Example Response:**
```json
{
  "schedule_version": "2026-02-01T00:00:00Z",
  "games": [
    {
      "game_id": "game-001",
      "home_team": "Team A",
      "away_team": "Team B",
      "start_time": "2026-02-01T14:00:00Z",
      "period_length_min": 15
    },
    {
      "game_id": "game-002",
      "home_team": "Team C",
      "away_team": "Team D",
      "start_time": "2026-02-01T15:00:00Z",
      "period_length_min": 15
    }
  ]
}
```

### 2. Post Events

**POST** `/v1/games/{game_id}/events`

Upload events from the mini PC to the cloud.

**Features:**
- Idempotent: Duplicate events (same `event_id`) are ignored
- Returns `acked_through` to indicate which events were successfully stored
- Supports batch uploads

**Example Request:**
```bash
curl -X POST "http://localhost:8001/v1/games/game-001/events" \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "rink-alpha-sheet1",
    "session_id": "session-uuid-123",
    "events": [
      {
        "event_id": "evt-001",
        "seq": 1,
        "type": "CLOCK_STARTED",
        "ts_local": "2026-02-01T14:05:00.000Z",
        "payload": {}
      },
      {
        "event_id": "evt-002",
        "seq": 2,
        "type": "GOAL",
        "ts_local": "2026-02-01T14:07:23.512Z",
        "payload": {"team": "home", "player": 12}
      }
    ]
  }'
```

**Example Response:**
```json
{
  "acked_through": 2,
  "server_time": "2026-02-01T14:07:25.000Z"
}
```

### 3. Post Heartbeat

**POST** `/v1/heartbeat`

Send operational status from mini PC for monitoring.

**Frequency:** Every 2-5 seconds (configurable)

**Example Request:**
```bash
curl -X POST "http://localhost:8001/v1/heartbeat" \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "rink-alpha-sheet1",
    "current_game_id": "game-001",
    "game_state": "RUNNING",
    "clock_running": true,
    "clock_value_ms": 352000,
    "last_event_seq": 143,
    "app_version": "1.4.2",
    "ts_local": "2026-02-01T14:10:00.000Z"
  }'
```

**Example Response:**
```json
{
  "status": "ok",
  "server_time": "2026-02-01T14:10:00.500Z"
}
```

## Admin/Debug Endpoints

### Get Latest Heartbeats

**GET** `/admin/heartbeats/latest`

View the most recent heartbeat from each device.

```bash
curl "http://localhost:8001/admin/heartbeats/latest"
```

### Get Game Events

**GET** `/admin/events/{game_id}`

View all events received for a specific game.

```bash
curl "http://localhost:8001/admin/events/game-001"
```

### Get All Game States

**GET** `/admin/games/state`

View the reconstructed state of all games based on received events. This endpoint replays all events for each game to show the current clock state, whether it's running, and how many events have been received.

Returns a styled HTML page for easy viewing in a browser.

```bash
# Open in browser
open http://localhost:8001/admin/games/state

# Or with curl
curl "http://localhost:8001/admin/games/state"
```

The page shows each game as a card with:
- Game ID and team names
- Large clock display showing time remaining
- Running/Paused status with color coding
- Period length and event count

## Database

The cloud API uses SQLite database: `cloud.db`

**Schema:**
- `rinks` - Rink/venue information
- `games` - Game schedules
- `received_events` - Events uploaded from mini PCs
- `heartbeats` - Device status/monitoring data
- `schedule_versions` - Track schedule changes

## Event Pushing from score-app

The score-app automatically pushes events to the cloud API using the `CloudEventPusher`. This runs in a separate process and continuously sends events as they're created.

**How it works:**
1. Events are stored in the local `game.db` database
2. The CloudEventPusher process monitors for new events
3. Events are sent via HTTP POST to `/v1/games/{game_id}/events`
4. Delivery status is tracked in the `deliveries` table
5. Failed deliveries are automatically retried
6. The UI shows cloud push status (healthy/pending/dead)

**Configuration:**
- Cloud API URL is set in `CLOUD_API_URL` constant in `cli.py`
- Default: `http://localhost:8001`
- Device ID is set to `device-001` by default

## Sample Data

The API automatically seeds sample data on startup:
- **Rink:** `rink-alpha` (Alpha Ice Arena)
- **Games:** 3 games scheduled for today
  - game-001: Team A vs Team B at 14:00 (15 min periods)
  - game-002: Team C vs Team D at 15:00 (15 min periods)
  - game-003: Team E vs Team F at 16:00 (20 min periods)

## Interactive API Documentation

FastAPI provides automatic interactive documentation:

- **Swagger UI:** http://localhost:8001/docs
- **ReDoc:** http://localhost:8001/redoc

## Testing

### Test Schedule Download
```bash
# Get today's schedule for rink-alpha
curl "http://localhost:8001/v1/rinks/rink-alpha/schedule"
```

### Test Event Upload
```bash
# Upload a test event
curl -X POST "http://localhost:8001/v1/games/game-001/events" \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "test-device",
    "session_id": "test-session",
    "events": [
      {
        "event_id": "test-evt-1",
        "seq": 1,
        "type": "GAME_STARTED",
        "ts_local": "2026-02-01T14:00:00.000Z",
        "payload": {}
      }
    ]
  }'

# View the uploaded events
curl "http://localhost:8001/admin/events/game-001"
```

### Test Heartbeat
```bash
curl -X POST "http://localhost:8001/v1/heartbeat" \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "test-device",
    "current_game_id": "game-001",
    "game_state": "IDLE",
    "app_version": "1.0.0",
    "ts_local": "2026-02-01T14:00:00.000Z"
  }'

# View latest heartbeats
curl "http://localhost:8001/admin/heartbeats/latest"
```

## Idempotency

The POST events endpoint is idempotent. Sending the same event multiple times (same `event_id`) will not create duplicates:

```bash
# Upload event first time - creates event
curl -X POST "http://localhost:8001/v1/games/game-001/events" \
  -H "Content-Type: application/json" \
  -d '{"device_id": "dev1", "session_id": "sess1", "events": [{"event_id": "e1", "seq": 1, "type": "TEST", "ts_local": "2026-02-01T14:00:00Z", "payload": {}}]}'

# Upload same event again - skips duplicate, still returns acked_through=1
curl -X POST "http://localhost:8001/v1/games/game-001/events" \
  -H "Content-Type: application/json" \
  -d '{"device_id": "dev1", "session_id": "sess1", "events": [{"event_id": "e1", "seq": 1, "type": "TEST", "ts_local": "2026-02-01T14:00:00Z", "payload": {}}]}'
```

## Use Cases

### Grafana Monitoring
The heartbeats table can be queried for:
- Device uptime monitoring
- Clock drift detection
- Game state visualization
- Alert on missing heartbeats

### Event Replay
All events are stored with sequence numbers, allowing:
- Game state reconstruction
- Audit trail
- Debugging
- Analytics

### Multi-Device Support
The system supports multiple mini PCs:
- Each device has unique `device_id`
- Multiple devices can send events to same game
- Heartbeats track each device independently
