"""
Cloud API Simulator for Scoreboard System

This module simulates the cloud backend that mini PCs connect to for:
1. Downloading game schedules
2. Uploading event logs
3. Sending heartbeats for monitoring
"""

import asyncio
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Path, Query, WebSocket
from pydantic import BaseModel
import uvicorn

# Set up logger
logger = logging.getLogger("score.cloud")


# ---------- Database Configuration ----------
from score.config import CloudConfig

CLOUD_DB_PATH = CloudConfig.DB_PATH


def get_db():
    """Get database connection."""
    conn = sqlite3.connect(CLOUD_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize cloud database schema."""
    logger.info("Initializing cloud database...")
    db = get_db()

    # Rinks and sheets
    db.execute("""
        CREATE TABLE IF NOT EXISTS rinks (
            rink_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)

    # Game schedules
    db.execute("""
        CREATE TABLE IF NOT EXISTS games (
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

    # Events received from mini PCs
    db.execute("""
        CREATE TABLE IF NOT EXISTS received_events (
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

    # Create index for idempotency checking
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_event_id
        ON received_events(event_id)
    """)

    # Heartbeats
    db.execute("""
        CREATE TABLE IF NOT EXISTS heartbeats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            current_game_id TEXT,
            game_state TEXT,
            clock_running INTEGER,
            clock_value_ms INTEGER,
            last_event_seq INTEGER,
            app_version TEXT,
            ts_local TEXT NOT NULL,
            received_at INTEGER NOT NULL
        )
    """)

    # Create index for latest heartbeat queries
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_heartbeat_device
        ON heartbeats(device_id, received_at DESC)
    """)

    # Schedule version tracking
    db.execute("""
        CREATE TABLE IF NOT EXISTS schedule_versions (
            rink_id TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (rink_id) REFERENCES rinks(rink_id)
        )
    """)

    db.commit()
    db.close()
    logger.info("Cloud database initialized")


init_db()


# ---------- Pydantic Models ----------

class Game(BaseModel):
    game_id: str
    home_team: str
    away_team: str
    start_time: str  # ISO 8601 format
    period_length_min: int


class ScheduleResponse(BaseModel):
    schedule_version: str
    games: list[Game]


class Event(BaseModel):
    event_id: str
    seq: int
    type: str
    ts_local: str  # ISO 8601 format
    payload: dict


class PostEventsRequest(BaseModel):
    device_id: str
    session_id: str
    events: list[Event]


class PostEventsResponse(BaseModel):
    acked_through: int
    server_time: str


class HeartbeatRequest(BaseModel):
    device_id: str
    current_game_id: Optional[str] = None
    game_state: Optional[str] = None
    clock_running: Optional[bool] = None
    clock_value_ms: Optional[int] = None
    last_event_seq: Optional[int] = None
    app_version: Optional[str] = None
    ts_local: str


class HeartbeatResponse(BaseModel):
    status: str
    server_time: str


# ---------- WebSocket state tracking ----------
websocket_clients = []


# ---------- Lifespan ----------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Starting cloud API...")

    # Log available endpoints
    logger.info("Available endpoints:")
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if methods and path:
            methods_str = ", ".join(sorted(methods - {"HEAD", "OPTIONS"}))
            if methods_str:  # Skip if only HEAD/OPTIONS
                logger.info(f"  {methods_str:20s} {path}")

    yield
    logger.info("Cloud API shutting down")


app = FastAPI(
    title="Scoreboard Cloud API Simulator",
    version="1.0.0",
    lifespan=lifespan
)


# ---------- API Endpoints ----------

@app.get("/v1/rinks/{rink_id}/schedule", response_model=ScheduleResponse)
async def get_schedule(
    rink_id: str = Path(..., description="Rink ID"),
    date: Optional[str] = Query(None, description="Date in YYYY-MM-DD format, defaults to today")
):
    """
    Download game schedule for a specific rink.

    Returns schedule_version and list of games for the specified date.
    """
    logger.info(f"Schedule request for rink_id={rink_id}, date={date}")

    # Default to today if no date provided
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    db = get_db()

    # Check if rink exists
    rink = db.execute("SELECT * FROM rinks WHERE rink_id = ?", (rink_id,)).fetchone()
    if not rink:
        db.close()
        raise HTTPException(status_code=404, detail=f"Rink {rink_id} not found")

    # Get schedule version
    version_row = db.execute(
        "SELECT version FROM schedule_versions WHERE rink_id = ?",
        (rink_id,)
    ).fetchone()

    schedule_version = version_row["version"] if version_row else datetime.now(timezone.utc).isoformat()

    # Query games for the date
    games = db.execute("""
        SELECT game_id, home_team, away_team, start_time, period_length_min
        FROM games
        WHERE rink_id = ? AND DATE(start_time) = ?
        ORDER BY start_time
    """, (rink_id, date)).fetchall()

    db.close()

    games_list = [
        Game(
            game_id=g["game_id"],
            home_team=g["home_team"],
            away_team=g["away_team"],
            start_time=g["start_time"],
            period_length_min=g["period_length_min"]
        )
        for g in games
    ]

    logger.info(f"Returning {len(games_list)} games for {rink_id} on {date}")

    return ScheduleResponse(
        schedule_version=schedule_version,
        games=games_list
    )


@app.post("/v1/games/{game_id}/events", response_model=PostEventsResponse)
async def post_events(
    game_id: str,
    request: PostEventsRequest
):
    """
    Receive events from mini PC with idempotency support.

    Returns acked_through to indicate which events were successfully stored.
    """
    logger.info(f"Received {len(request.events)} events for game {game_id} from device {request.device_id}")

    db = get_db()

    # Verify game exists
    game = db.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if not game:
        db.close()
        raise HTTPException(status_code=404, detail=f"Game {game_id} not found")

    acked_through = 0
    current_time = int(time.time())
    has_new_events = False

    # Process events with idempotency
    for event in sorted(request.events, key=lambda e: e.seq):
        try:
            # Check if event already exists (idempotency)
            existing = db.execute(
                "SELECT seq FROM received_events WHERE event_id = ?",
                (event.event_id,)
            ).fetchone()

            if existing:
                logger.debug(f"Event {event.event_id} already exists, skipping")
                acked_through = event.seq
                continue

            # Insert new event
            db.execute("""
                INSERT INTO received_events (
                    game_id, device_id, session_id, event_id, seq, type,
                    ts_local, payload, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                game_id,
                request.device_id,
                request.session_id,
                event.event_id,
                event.seq,
                event.type,
                event.ts_local,
                json.dumps(event.payload),
                current_time
            ))

            acked_through = event.seq
            has_new_events = True
            logger.debug(f"Stored event {event.event_id} (seq={event.seq}, type={event.type})")

        except sqlite3.IntegrityError as e:
            logger.warning(f"Integrity error for event {event.event_id}: {e}")
            # Event already exists, continue
            acked_through = event.seq
            continue

    db.commit()
    db.close()

    # Notify WebSocket clients if there were new events
    if has_new_events and websocket_clients:
        await notify_game_state_change()

    server_time = datetime.now(timezone.utc).isoformat()

    logger.info(f"Acknowledged events through seq={acked_through} for game {game_id}")

    return PostEventsResponse(
        acked_through=acked_through,
        server_time=server_time
    )


async def notify_game_state_change():
    """Notify all connected WebSocket clients that game state has changed."""
    dead_clients = []
    for ws in websocket_clients:
        try:
            await ws.send_text("update")
        except:
            dead_clients.append(ws)

    # Remove disconnected clients
    for ws in dead_clients:
        websocket_clients.remove(ws)

    if dead_clients:
        logger.debug(f"Removed {len(dead_clients)} disconnected WebSocket client(s)")


@app.post("/v1/heartbeat", response_model=HeartbeatResponse)
async def post_heartbeat(request: HeartbeatRequest):
    """
    Receive heartbeat from mini PC for monitoring.

    Used for Grafana dashboards and alerts.
    """
    logger.debug(f"Heartbeat from device {request.device_id}")

    db = get_db()

    current_time = int(time.time())

    db.execute("""
        INSERT INTO heartbeats (
            device_id, current_game_id, game_state, clock_running,
            clock_value_ms, last_event_seq, app_version, ts_local, received_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        request.device_id,
        request.current_game_id,
        request.game_state,
        1 if request.clock_running else 0 if request.clock_running is not None else None,
        request.clock_value_ms,
        request.last_event_seq,
        request.app_version,
        request.ts_local,
        current_time
    ))

    db.commit()
    db.close()

    server_time = datetime.now(timezone.utc).isoformat()

    return HeartbeatResponse(
        status="ok",
        server_time=server_time
    )


# ---------- Admin/Debug Endpoints ----------

@app.get("/admin/heartbeats/latest")
async def get_latest_heartbeats():
    """Get latest heartbeat from each device for monitoring."""
    db = get_db()

    # Get latest heartbeat per device
    heartbeats = db.execute("""
        SELECT h1.*
        FROM heartbeats h1
        INNER JOIN (
            SELECT device_id, MAX(received_at) as max_time
            FROM heartbeats
            GROUP BY device_id
        ) h2 ON h1.device_id = h2.device_id AND h1.received_at = h2.max_time
        ORDER BY h1.received_at DESC
    """).fetchall()

    db.close()

    return {
        "heartbeats": [dict(h) for h in heartbeats]
    }


@app.get("/admin/events/{game_id}")
async def get_game_events(game_id: str):
    """Get all events for a specific game."""
    db = get_db()

    events = db.execute("""
        SELECT * FROM received_events
        WHERE game_id = ?
        ORDER BY seq
    """, (game_id,)).fetchall()

    db.close()

    return {
        "game_id": game_id,
        "event_count": len(events),
        "events": [dict(e) for e in events]
    }


def reconstruct_game_state(game_id: str):
    """
    Reconstruct game state from received events.

    Args:
        game_id: Game ID to reconstruct state for

    Returns:
        dict with game state information
    """
    from score.state import load_game_state_from_db

    db = get_db()

    # Get game metadata
    game = db.execute(
        "SELECT * FROM games WHERE game_id = ?",
        (game_id,)
    ).fetchone()

    if not game:
        db.close()
        return None

    db.close()

    # Use shared replay logic
    result = load_game_state_from_db(CLOUD_DB_PATH, game_id)

    return {
        "game_id": game_id,
        "home_team": game["home_team"],
        "away_team": game["away_team"],
        "start_time": game["start_time"],
        "period_length_min": game["period_length_min"],
        "clock_seconds": result["seconds"],
        "clock_running": result["running"],
        "event_count": result["num_events"],
        "last_update": result["last_update"]
    }


@app.get("/admin/games/state")
async def get_all_game_states(format: Optional[str] = Query(None, description="Response format: 'json' or 'html'")):
    """
    Get current state of all games based on received events.

    This endpoint reconstructs game state by replaying all events for each game.
    Returns HTML for browser viewing or JSON if format=json parameter is provided.
    """
    from fastapi.responses import HTMLResponse

    db = get_db()

    # Get all games
    games = db.execute("SELECT game_id FROM games ORDER BY start_time").fetchall()

    db.close()

    game_states = []
    for game_row in games:
        game_id = game_row["game_id"]
        state = reconstruct_game_state(game_id)
        if state:
            game_states.append(state)

    # Return JSON if requested
    if format == "json":
        return {
            "game_count": len(game_states),
            "games": game_states
        }

    # Generate HTML view with JavaScript auto-update
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Game States</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
                color: #fff;
            }
            .container { max-width: 1200px; margin: 0 auto; }
            h1 {
                text-align: center;
                margin-bottom: 30px;
                font-size: 2.5em;
                text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
            }
            .games-grid {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
                gap: 20px;
            }
            .game-card {
                background: rgba(255, 255, 255, 0.15);
                backdrop-filter: blur(10px);
                border-radius: 15px;
                padding: 20px;
                border: 2px solid rgba(255, 255, 255, 0.2);
                box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
                transition: transform 0.2s ease;
            }
            .game-card:hover {
                transform: translateY(-5px);
            }
            .game-id {
                font-size: 0.85em;
                opacity: 0.7;
                margin-bottom: 10px;
            }
            .teams {
                font-size: 1.3em;
                font-weight: 600;
                margin-bottom: 15px;
            }
            .clock {
                font-size: 3em;
                font-weight: 700;
                text-align: center;
                margin: 20px 0;
                font-variant-numeric: tabular-nums;
            }
            .status {
                text-align: center;
                font-size: 1.1em;
                margin-bottom: 15px;
            }
            .status.running { color: #4ade80; }
            .status.paused { color: #fbbf24; }
            .info {
                display: flex;
                justify-content: space-between;
                font-size: 0.9em;
                opacity: 0.8;
                margin-top: 10px;
                padding-top: 10px;
                border-top: 1px solid rgba(255, 255, 255, 0.2);
            }
            .no-games {
                grid-column: 1/-1;
                text-align: center;
                padding: 40px;
                font-size: 1.5em;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎮 Game States</h1>
            <div class="games-grid" id="gamesGrid">
                <div class="no-games">Loading...</div>
            </div>
        </div>

        <script>
        function formatClock(seconds) {
            const mins = Math.floor(seconds / 60);
            const secs = seconds % 60;
            return `${mins}:${secs.toString().padStart(2, '0')}`;
        }

        function updateGameStates() {
            fetch('/admin/games/state?format=json')
                .then(response => response.json())
                .then(data => {
                    const grid = document.getElementById('gamesGrid');

                    if (data.games.length === 0) {
                        grid.innerHTML = '<div class="no-games">No games found</div>';
                        return;
                    }

                    let html = '';
                    data.games.forEach(game => {
                        const status = game.clock_running ? 'running' : 'paused';
                        const statusIcon = game.clock_running ? '▶' : '⏸';
                        const clock = formatClock(game.clock_seconds);

                        html += `
                            <div class="game-card">
                                <div class="game-id">${game.game_id}</div>
                                <div class="teams">${game.home_team} vs ${game.away_team}</div>
                                <div class="clock">${clock}</div>
                                <div class="status ${status}">${statusIcon} ${status.toUpperCase()}</div>
                                <div class="info">
                                    <span>Period: ${game.period_length_min} min</span>
                                    <span>Events: ${game.event_count}</span>
                                </div>
                            </div>
                        `;
                    });

                    grid.innerHTML = html;
                })
                .catch(error => {
                    console.error('Error fetching game states:', error);
                });
        }

        // Connect to WebSocket for real-time updates
        const ws = new WebSocket(`ws://${location.host}/ws/game-states`);

        ws.onopen = () => {
            console.log('WebSocket connected - will update on database changes');
            // Load initial state
            updateGameStates();
        };

        ws.onmessage = (event) => {
            // Server sends "update" when database changes
            if (event.data === 'update') {
                console.log('Database changed - updating game states');
                updateGameStates();
            }
        };

        ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };

        ws.onclose = () => {
            console.log('WebSocket disconnected - attempting to reconnect...');
            // Attempt to reconnect after 2 seconds
            setTimeout(() => {
                location.reload();
            }, 2000);
        };
        </script>
    </body>
    </html>
    """

    return HTMLResponse(content=html)


@app.websocket("/ws/game-states")
async def websocket_game_states(websocket: WebSocket):
    """WebSocket endpoint for real-time game state updates."""
    await websocket.accept()
    websocket_clients.append(websocket)
    logger.info(f"WebSocket client connected for game states (total: {len(websocket_clients)})")

    try:
        # Keep connection alive
        while True:
            # Wait for messages (we don't expect any from client, but this keeps connection alive)
            await asyncio.sleep(3600)
    except:
        pass
    finally:
        if websocket in websocket_clients:
            websocket_clients.remove(websocket)
        logger.info(f"WebSocket client disconnected for game states (total: {len(websocket_clients)})")


# ---------- Data Seeding ----------

def seed_sample_data():
    """Seed the database with sample data for testing."""
    logger.info("Seeding sample data...")
    db = get_db()

    current_time = int(time.time())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Add sample rink
    db.execute("""
        INSERT OR IGNORE INTO rinks (rink_id, name, created_at)
        VALUES ('rink-alpha', 'Alpha Ice Arena', ?)
    """, (current_time,))

    # Add sample games
    games = [
        ("game-001", "rink-alpha", "Team A", "Team B", f"{today}T14:00:00Z", 15),
        ("game-002", "rink-alpha", "Team C", "Team D", f"{today}T15:00:00Z", 15),
        ("game-003", "rink-alpha", "Team E", "Team F", f"{today}T16:00:00Z", 20),
    ]

    for game in games:
        db.execute("""
            INSERT OR IGNORE INTO games (
                game_id, rink_id, home_team, away_team, start_time,
                period_length_min, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (*game, current_time))

    # Update schedule version
    version = datetime.now(timezone.utc).isoformat()
    db.execute("""
        INSERT OR REPLACE INTO schedule_versions (rink_id, version, updated_at)
        VALUES ('rink-alpha', ?, ?)
    """, (version, current_time))

    db.commit()
    db.close()

    logger.info("Sample data seeded successfully")


def main():
    """Run the cloud API server."""
    # Configure logging first
    from score.log import init_logging
    init_logging("cloud", color="dim magenta")

    logger.info("Starting Cloud API Simulator")

    # Seed sample data on startup
    seed_sample_data()

    # Run on a different port than the main app (8001 instead of 8000)
    logger.info(f"Starting cloud API server on http://{CloudConfig.HOST}:{CloudConfig.PORT}")
    uvicorn.run(app, host=CloudConfig.HOST, port=CloudConfig.PORT, log_config=None)


if __name__ == "__main__":
    main()
