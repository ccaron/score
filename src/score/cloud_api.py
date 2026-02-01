"""
Cloud API Simulator for Scoreboard System

This module simulates the cloud backend that mini PCs connect to for:
1. Downloading game schedules
2. Uploading event logs
3. Sending heartbeats for monitoring
"""

import json
import logging
import logging.handlers
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel
from rich.console import ConsoleRenderable
from rich.logging import RichHandler
import uvicorn

# Set up logger
logger = logging.getLogger(__name__)


class RichHandlerWithLoggerName(RichHandler):
    """Custom RichHandler that displays logger name in the path."""

    def render(
        self,
        *,
        record: logging.LogRecord,
        traceback,
        message_renderable: ConsoleRenderable,
    ):
        # Add logger name to the path
        path = f"{record.name}"
        record.pathname = path
        record.filename = path
        record.lineno = 0
        return super().render(
            record=record,
            traceback=traceback,
            message_renderable=message_renderable,
        )


def init_logging():
    """Configure Rich logging with process/thread info and logger names."""
    logging.basicConfig(
        level=logging.INFO,
        format="[dim magenta][PID: %(process)d TID: %(thread)d][/dim magenta] %(message)s",
        datefmt="[%X]",
        handlers=[RichHandlerWithLoggerName(markup=True)],
        force=True,
    )

    # Configure uvicorn's loggers to use our Rich handler
    for logger_name in ["uvicorn", "uvicorn.error"]:
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    # Reduce noise from access logs (comment out if you want to see all requests)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# ---------- Database Configuration ----------
CLOUD_DB_PATH = "cloud.db"


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


# ---------- Lifespan ----------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Starting cloud API...")
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
            logger.debug(f"Stored event {event.event_id} (seq={event.seq}, type={event.type})")

        except sqlite3.IntegrityError as e:
            logger.warning(f"Integrity error for event {event.event_id}: {e}")
            # Event already exists, continue
            acked_through = event.seq
            continue

    db.commit()
    db.close()

    server_time = datetime.now(timezone.utc).isoformat()

    logger.info(f"Acknowledged events through seq={acked_through} for game {game_id}")

    return PostEventsResponse(
        acked_through=acked_through,
        server_time=server_time
    )


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
    init_logging()

    logger.info("Starting Cloud API Simulator")

    # Seed sample data on startup
    seed_sample_data()

    # Run on a different port than the main app (8001 instead of 8000)
    uvicorn.run(app, host="0.0.0.0", port=8001, log_config=None)


if __name__ == "__main__":
    main()
