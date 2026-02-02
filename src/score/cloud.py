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

import requests
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

    # Devices (mini PCs / score-app installations)
    db.execute("""
        CREATE TABLE IF NOT EXISTS devices (
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

    # Game schedules
    db.execute("""
        CREATE TABLE IF NOT EXISTS games (
            game_id TEXT PRIMARY KEY,
            rink_id TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            home_abbrev TEXT,
            away_abbrev TEXT,
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

    # Players table (master player data from NHL API)
    db.execute("""
        CREATE TABLE IF NOT EXISTS players (
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

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_players_name
        ON players(last_name, first_name)
    """)

    # Teams table (master team data)
    db.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            team_abbrev TEXT PRIMARY KEY,
            city TEXT NOT NULL,
            team_name TEXT NOT NULL,
            full_name TEXT NOT NULL,
            conference TEXT,
            division TEXT,
            created_at INTEGER NOT NULL
        )
    """)

    # Team rosters (temporal tracking - when players joined/left teams)
    db.execute("""
        CREATE TABLE IF NOT EXISTS team_rosters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            team_abbrev TEXT NOT NULL,
            roster_status TEXT NOT NULL,
            added_at INTEGER NOT NULL,
            removed_at INTEGER,
            FOREIGN KEY (player_id) REFERENCES players(player_id),
            UNIQUE(player_id, team_abbrev, added_at)
        )
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_team_rosters_team
        ON team_rosters(team_abbrev, added_at)
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_team_rosters_player
        ON team_rosters(player_id)
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


class DeviceConfigResponse(BaseModel):
    device_id: str
    is_assigned: bool
    rink_id: Optional[str] = None
    sheet_name: Optional[str] = None
    device_name: Optional[str] = None
    message: Optional[str] = None


class DeviceInfo(BaseModel):
    device_id: str
    rink_id: Optional[str] = None
    sheet_name: Optional[str] = None
    device_name: Optional[str] = None
    is_assigned: bool
    first_seen_at: int
    last_seen_at: int
    notes: Optional[str] = None


class CreateDeviceRequest(BaseModel):
    device_id: str
    rink_id: Optional[str] = None
    sheet_name: Optional[str] = None
    device_name: Optional[str] = None
    notes: Optional[str] = None


class CreateRinkRequest(BaseModel):
    rink_id: str
    name: str


class AssignDeviceRequest(BaseModel):
    rink_id: str
    sheet_name: str
    device_name: Optional[str] = None
    notes: Optional[str] = None


class UpdateDeviceRequest(BaseModel):
    rink_id: Optional[str] = None
    sheet_name: Optional[str] = None
    device_name: Optional[str] = None
    notes: Optional[str] = None


class DeviceListResponse(BaseModel):
    devices: list[DeviceInfo]


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
    date: Optional[str] = Query(None, description="Date in YYYY-MM-DD format (defaults to today)")
):
    """
    Download game schedule for a specific rink.

    Returns schedule_version and games for the specified date (defaults to today).
    """
    logger.info(f"Schedule request for rink_id={rink_id}, date={date}")

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

    # Default to today if no date specified (use local timezone, not UTC)
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    # For Pacific timezone (UTC-8/7), we need to query a wider range
    # A game on Feb 1 Pacific could be stored as Feb 2 UTC if it's an evening game
    # So we query for both the requested date and the next day in UTC
    from datetime import timedelta
    date_obj = datetime.strptime(date, "%Y-%m-%d")
    next_date = (date_obj + timedelta(days=1)).strftime("%Y-%m-%d")

    # Query games for the rink on the specified date OR next date (to catch evening games)
    # Match games where start_time begins with either date
    games = db.execute("""
        SELECT game_id, home_team, away_team, start_time, period_length_min
        FROM games
        WHERE rink_id = ? AND (start_time LIKE ? OR start_time LIKE ?)
        ORDER BY start_time
    """, (rink_id, f"{date}%", f"{next_date}%")).fetchall()

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


@app.get("/v1/games/{game_id}/roster")
async def get_game_roster(game_id: str = Path(..., description="Game ID")):
    """
    Get roster for a game as of game start time.

    Returns home and away rosters with full player details.
    """
    logger.info(f"Roster request for game_id={game_id}")

    db = get_db()

    # Get game start time
    game = db.execute(
        "SELECT start_time FROM games WHERE game_id = ?",
        (game_id,)
    ).fetchone()

    if not game:
        db.close()
        raise HTTPException(status_code=404, detail="Game not found")

    # Parse start time to unix timestamp
    start_time = int(datetime.fromisoformat(game["start_time"]).timestamp())

    db.close()

    # Get roster state at game start using state replay
    from score.state import get_game_roster_at_time
    roster_state = get_game_roster_at_time(CLOUD_DB_PATH, game_id, start_time)

    return {
        "game_id": game_id,
        "home_roster": roster_state["home_roster"],
        "away_roster": roster_state["away_roster"],
        "players": roster_state["roster_details"]
    }


@app.get("/v1/devices/{device_id}/config", response_model=DeviceConfigResponse)
async def get_device_config(device_id: str = Path(..., description="Device ID")):
    """
    Get configuration for a device.

    Returns device assignment (rink_id, sheet_name) if assigned,
    or registers the device as unassigned if first time seeing it.
    """
    logger.info(f"Config request from device_id={device_id}")

    db = get_db()
    current_time = int(time.time())

    # Check if device exists
    device = db.execute(
        "SELECT * FROM devices WHERE device_id = ?",
        (device_id,)
    ).fetchone()

    if device:
        # Update last_seen_at
        db.execute(
            "UPDATE devices SET last_seen_at = ? WHERE device_id = ?",
            (current_time, device_id)
        )
        db.commit()

        is_assigned = bool(device["is_assigned"])

        if is_assigned:
            logger.info(f"Device {device_id} is assigned to rink={device['rink_id']}, sheet={device['sheet_name']}")
            db.close()
            return DeviceConfigResponse(
                device_id=device_id,
                is_assigned=True,
                rink_id=device["rink_id"],
                sheet_name=device["sheet_name"],
                device_name=device["device_name"],
                message=f"Assigned to {device['rink_id']} - {device['sheet_name']}"
            )
        else:
            logger.info(f"Device {device_id} exists but is not assigned")
            db.close()
            return DeviceConfigResponse(
                device_id=device_id,
                is_assigned=False,
                message="Device registered but not assigned. Please contact admin to assign this device."
            )
    else:
        # First time seeing this device - register it as unassigned
        logger.info(f"New device {device_id} - registering as unassigned")
        db.execute("""
            INSERT INTO devices (device_id, is_assigned, first_seen_at, last_seen_at)
            VALUES (?, 0, ?, ?)
        """, (device_id, current_time, current_time))
        db.commit()
        db.close()

        return DeviceConfigResponse(
            device_id=device_id,
            is_assigned=False,
            message="Device registered. Please contact admin to assign this device to a rink and sheet."
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

@app.post("/admin/rinks")
async def create_rink(request: CreateRinkRequest):
    """
    Create a new rink.

    Args:
        rink_id: Unique identifier for the rink (e.g., "rink-alpha")
        name: Human-readable name (e.g., "Alpha Ice Arena")
    """
    logger.info(f"Creating rink {request.rink_id}")

    db = get_db()
    current_time = int(time.time())

    # Check if rink already exists
    existing = db.execute(
        "SELECT rink_id FROM rinks WHERE rink_id = ?",
        (request.rink_id,)
    ).fetchone()

    if existing:
        db.close()
        raise HTTPException(
            status_code=409,
            detail=f"Rink {request.rink_id} already exists"
        )

    # Insert rink
    db.execute("""
        INSERT INTO rinks (rink_id, name, created_at)
        VALUES (?, ?, ?)
    """, (request.rink_id, request.name, current_time))

    db.commit()
    db.close()

    logger.info(f"Successfully created rink {request.rink_id}")

    return {
        "status": "ok",
        "message": f"Rink {request.rink_id} created",
        "rink": {
            "rink_id": request.rink_id,
            "name": request.name
        }
    }


@app.put("/admin/rinks/{rink_id}")
async def update_rink(rink_id: str, request: dict):
    """
    Update a rink's name.
    """
    logger.info(f"Updating rink {rink_id}")

    db = get_db()

    # Check if rink exists
    rink = db.execute(
        "SELECT rink_id FROM rinks WHERE rink_id = ?",
        (rink_id,)
    ).fetchone()

    if not rink:
        db.close()
        raise HTTPException(status_code=404, detail=f"Rink {rink_id} not found")

    new_name = request.get("name")
    if not new_name:
        db.close()
        raise HTTPException(status_code=400, detail="Name is required")

    # Update rink name
    db.execute(
        "UPDATE rinks SET name = ? WHERE rink_id = ?",
        (new_name, rink_id)
    )

    db.commit()
    db.close()

    logger.info(f"Successfully updated rink {rink_id} name to {new_name}")

    return {
        "status": "ok",
        "message": f"Rink {rink_id} updated",
        "rink": {
            "rink_id": rink_id,
            "name": new_name
        }
    }


@app.delete("/admin/rinks/{rink_id}")
async def delete_rink(rink_id: str):
    """
    Delete a rink.

    This will fail if there are devices assigned to this rink.
    """
    logger.info(f"Deleting rink {rink_id}")

    db = get_db()

    # Check if rink exists
    rink = db.execute(
        "SELECT rink_id FROM rinks WHERE rink_id = ?",
        (rink_id,)
    ).fetchone()

    if not rink:
        db.close()
        raise HTTPException(status_code=404, detail=f"Rink {rink_id} not found")

    # Check if any devices are assigned to this rink
    devices = db.execute(
        "SELECT COUNT(*) as count FROM devices WHERE rink_id = ? AND is_assigned = 1",
        (rink_id,)
    ).fetchone()

    if devices["count"] > 0:
        db.close()
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete rink {rink_id}: {devices['count']} device(s) are assigned to it. Unassign devices first."
        )

    # Delete the rink
    db.execute("DELETE FROM rinks WHERE rink_id = ?", (rink_id,))

    db.commit()
    db.close()

    logger.info(f"Successfully deleted rink {rink_id}")

    return {
        "status": "ok",
        "message": f"Rink {rink_id} deleted"
    }


@app.get("/admin/devices")
async def list_devices(format: Optional[str] = Query(None, description="Response format: 'json' or 'html'")):
    """
    List all registered devices and their assignments.

    Returns HTML admin UI by default, or JSON if format=json is specified.
    """
    db = get_db()

    devices = db.execute("""
        SELECT device_id, rink_id, sheet_name, device_name, is_assigned,
               first_seen_at, last_seen_at, notes
        FROM devices
        ORDER BY last_seen_at DESC
    """).fetchall()

    # Get available rinks for dropdown
    rinks = db.execute("SELECT rink_id, name FROM rinks ORDER BY name").fetchall()

    db.close()

    device_list = [
        {
            "device_id": d["device_id"],
            "rink_id": d["rink_id"],
            "sheet_name": d["sheet_name"],
            "device_name": d["device_name"],
            "is_assigned": bool(d["is_assigned"]),
            "first_seen_at": d["first_seen_at"],
            "last_seen_at": d["last_seen_at"],
            "notes": d["notes"]
        }
        for d in devices
    ]

    # Return JSON if requested
    if format == "json":
        return DeviceListResponse(devices=[DeviceInfo(**d) for d in device_list])

    # Return HTML admin UI
    from fastapi.responses import HTMLResponse
    import datetime

    def format_timestamp(ts):
        if ts:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        return "Never"

    rink_options = "".join([f'<option value="{r["rink_id"]}">{r["name"]} ({r["rink_id"]})</option>' for r in rinks])

    rinks_list = [{"rink_id": r["rink_id"], "name": r["name"]} for r in rinks]

    devices_html = ""
    for d in device_list:
        status_badge = '<span class="badge assigned">Assigned</span>' if d["is_assigned"] else '<span class="badge unassigned">Not Assigned</span>'

        devices_html += f"""
        <tr data-device-id="{d['device_id']}">
            <td class="device-id">{d['device_id']}</td>
            <td>
                <select class="rink-select" data-device-id="{d['device_id']}">
                    <option value="">-- Select Rink --</option>
                    {rink_options}
                </select>
                <script>
                document.querySelector('select.rink-select[data-device-id="{d["device_id"]}"]').value = "{d["rink_id"] or ""}";
                </script>
            </td>
            <td><input type="text" class="sheet-input" data-device-id="{d['device_id']}" value="{d['sheet_name'] or ''}" placeholder="Sheet 1"></td>
            <td><input type="text" class="name-input" data-device-id="{d['device_id']}" value="{d['device_name'] or ''}" placeholder="Display name"></td>
            <td>{status_badge}</td>
            <td class="timestamp">{format_timestamp(d['last_seen_at'])}</td>
            <td class="actions">
                <button class="btn-save" onclick="saveDevice('{d['device_id']}')">Save</button>
                <button class="btn-unassign" onclick="unassignDevice('{d['device_id']}')">Unassign</button>
                <button class="btn-delete" onclick="deleteDevice('{d['device_id']}')">Delete</button>
            </td>
        </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Device Management</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }}
            .nav {{
                background: rgba(255, 255, 255, 0.95);
                padding: 15px 30px;
                border-radius: 10px;
                margin-bottom: 20px;
                box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
            }}
            .nav a {{
                color: #667eea;
                text-decoration: none;
                margin-right: 20px;
                font-weight: 500;
            }}
            .nav a:hover {{
                text-decoration: underline;
            }}
            .nav a.active {{
                color: #764ba2;
                font-weight: 700;
            }}
            .container {{
                max-width: 1400px;
                margin: 0 auto;
                background: white;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
                overflow: hidden;
            }}
            .header {{
                padding: 30px;
                border-bottom: 1px solid #e9ecef;
            }}
            .header h1 {{
                font-size: 1.8em;
                margin-bottom: 5px;
                color: #333;
            }}
            .header p {{
                color: #6c757d;
                font-size: 0.95em;
            }}
            .content {{ padding: 30px; }}
            .rink-section {{
                margin-bottom: 30px;
            }}
            .rink-section h3 {{
                font-size: 1.1em;
                margin-bottom: 15px;
                color: #495057;
            }}
            .add-rink-form {{
                display: flex;
                gap: 10px;
                align-items: flex-end;
            }}
            .add-rink-form input {{
                padding: 8px 12px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                font-size: 0.9em;
            }}
            .add-rink-form button {{
                padding: 8px 16px;
                background: #667eea;
                color: white;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 0.9em;
            }}
            .add-rink-form button:hover {{
                background: #5568d3;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 20px;
            }}
            th {{
                background: #f8f9fa;
                padding: 15px;
                text-align: left;
                font-weight: 600;
                color: #495057;
                border-bottom: 2px solid #dee2e6;
            }}
            td {{
                padding: 12px 15px;
                border-bottom: 1px solid #e9ecef;
            }}
            tr:hover {{ background: #f8f9fa; }}
            .device-id {{
                font-family: 'Courier New', monospace;
                font-weight: 600;
                color: #667eea;
            }}
            input, select, textarea {{
                width: 100%;
                padding: 8px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                font-size: 14px;
            }}
            textarea {{
                resize: vertical;
                min-height: 40px;
                font-family: inherit;
            }}
            input:focus, select:focus, textarea:focus {{
                outline: none;
                border-color: #667eea;
                box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
            }}
            .badge {{
                display: inline-block;
                padding: 4px 10px;
                border-radius: 4px;
                font-size: 12px;
                font-weight: 500;
            }}
            .badge.assigned {{
                background: #d4edda;
                color: #155724;
            }}
            .badge.unassigned {{
                background: #fff3cd;
                color: #856404;
            }}
            .timestamp {{
                font-size: 13px;
                color: #6c757d;
            }}
            .actions {{
                display: flex;
                gap: 5px;
            }}
            button {{
                padding: 6px 12px;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 13px;
            }}
            .btn-save {{
                background: #28a745;
                color: white;
            }}
            .btn-save:hover {{
                background: #218838;
            }}
            .btn-unassign {{
                background: #dc3545;
                color: white;
            }}
            .btn-unassign:hover {{
                background: #c82333;
            }}
            .btn-delete {{
                background: #6c757d;
                color: white;
            }}
            .btn-delete:hover {{
                background: #5a6268;
            }}
            .message {{
                padding: 12px;
                border-radius: 4px;
                margin-bottom: 20px;
                display: none;
            }}
            .message.success {{
                background: #d4edda;
                color: #155724;
                border: 1px solid #c3e6cb;
            }}
            .message.error {{
                background: #f8d7da;
                color: #721c24;
                border: 1px solid #f5c6cb;
            }}
            .hint {{
                margin-bottom: 15px;
                color: #6c757d;
                font-size: 0.9em;
            }}
        </style>
    </head>
    <body>
        <div class="nav">
            <a href="/admin/devices" class="active">Devices</a>
            <a href="/admin/games/state">Games</a>
            <a href="/admin/teams">Teams</a>
            <a href="/admin/players">Players</a>
            <a href="/admin/rosters">Rosters</a>
        </div>
        <div class="container">
            <div class="header">
                <h1>Device Management</h1>
                <p>Manage device assignments for score-app installations</p>
            </div>

            <div class="content">
                <div id="message" class="message"></div>

                <div class="rink-section">
                    <h3>Rinks</h3>

                    <table style="margin-bottom: 20px;">
                        <thead>
                            <tr>
                                <th style="width: 30%;">Rink ID</th>
                                <th style="width: 50%;">Name</th>
                                <th style="width: 20%;">Actions</th>
                            </tr>
                            <tr class="filter-row">
                                <td><input type="text" id="filterRinkId" placeholder="Filter..." onkeyup="filterRinksTable()"></td>
                                <td><input type="text" id="filterRinkName" placeholder="Filter..." onkeyup="filterRinksTable()"></td>
                                <td></td>
                            </tr>
                        </thead>
                        <tbody id="rinksTableBody">
                            {''.join([f'''
                                <tr data-rink-id="{r["rink_id"]}">
                                    <td class="device-id">{r["rink_id"]}</td>
                                    <td><input type="text" class="rink-name-input" data-rink-id="{r["rink_id"]}" value="{r["name"]}" /></td>
                                    <td class="actions">
                                        <button class="btn-save" onclick="saveRink('{r["rink_id"]}')">Save</button>
                                        <button class="btn-delete" onclick="deleteRink('{r["rink_id"]}')">Delete</button>
                                    </td>
                                </tr>
                            ''' for r in rinks_list]) if rinks_list else '<tr><td colspan="3" style="text-align: center; color: #6c757d; padding: 20px;">No rinks yet</td></tr>'}
                        </tbody>
                    </table>

                    <div class="add-rink-form">
                        <input type="text" id="addRinkId" placeholder="Rink ID (e.g., rink-alpha)" style="width: 200px;">
                        <input type="text" id="addRinkName" placeholder="Rink Name (e.g., Alpha Arena)" style="width: 250px;">
                        <button onclick="addRink()">Add Rink</button>
                    </div>
                </div>

                <div class="hint">
                    Devices automatically register when they connect. Assign them to rinks and sheets below.
                </div>

                <table>
                    <thead>
                        <tr>
                            <th style="width: 20%;">Device ID</th>
                            <th>Rink</th>
                            <th>Sheet Name</th>
                            <th>Device Name</th>
                            <th>Status</th>
                            <th>Last Seen</th>
                            <th>Actions</th>
                        </tr>
                        <tr class="filter-row">
                            <td><input type="text" id="filterDeviceId" placeholder="Filter..." onkeyup="filterDeviceTable()"></td>
                            <td><input type="text" id="filterRink" placeholder="Filter..." onkeyup="filterDeviceTable()"></td>
                            <td><input type="text" id="filterSheet" placeholder="Filter..." onkeyup="filterDeviceTable()"></td>
                            <td><input type="text" id="filterDeviceName" placeholder="Filter..." onkeyup="filterDeviceTable()"></td>
                            <td><input type="text" id="filterStatus" placeholder="Filter..." onkeyup="filterDeviceTable()"></td>
                            <td><input type="text" id="filterLastSeen" placeholder="Filter..." onkeyup="filterDeviceTable()"></td>
                            <td></td>
                        </tr>
                    </thead>
                    <tbody>
                        {devices_html}
                    </tbody>
                </table>
            </div>
        </div>

        <script>
        function filterDeviceTable() {{
            const filters = {{
                deviceId: document.getElementById('filterDeviceId').value.toLowerCase(),
                rink: document.getElementById('filterRink').value.toLowerCase(),
                sheet: document.getElementById('filterSheet').value.toLowerCase(),
                deviceName: document.getElementById('filterDeviceName').value.toLowerCase(),
                status: document.getElementById('filterStatus').value.toLowerCase(),
                lastSeen: document.getElementById('filterLastSeen').value.toLowerCase()
            }};

            const tbody = document.querySelector('table tbody');
            const rows = tbody.getElementsByTagName('tr');

            for (let i = 0; i < rows.length; i++) {{
                const cells = rows[i].getElementsByTagName('td');
                if (cells.length < 7) continue;

                const deviceId = cells[0].textContent.toLowerCase();
                const rink = cells[1].querySelector('select')?.value.toLowerCase() || '';
                const sheet = cells[2].querySelector('input')?.value.toLowerCase() || '';
                const deviceName = cells[3].querySelector('input')?.value.toLowerCase() || '';
                const status = cells[4].textContent.toLowerCase();
                const lastSeen = cells[5].textContent.toLowerCase();

                const match =
                    deviceId.includes(filters.deviceId) &&
                    rink.includes(filters.rink) &&
                    sheet.includes(filters.sheet) &&
                    deviceName.includes(filters.deviceName) &&
                    status.includes(filters.status) &&
                    lastSeen.includes(filters.lastSeen);

                rows[i].style.display = match ? '' : 'none';
            }}
        }}

        function showMessage(text, type) {{
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = `message ${{type}}`;
            msg.style.display = 'block';
            setTimeout(() => {{
                msg.style.display = 'none';
            }}, 5000);
        }}

        async function saveDevice(deviceId) {{
            const row = document.querySelector(`tr[data-device-id="${{deviceId}}"]`);
            const rinkId = row.querySelector('.rink-select').value;
            const sheetName = row.querySelector('.sheet-input').value;
            const deviceName = row.querySelector('.name-input').value;

            if (!rinkId || !sheetName) {{
                showMessage('Please select a rink and enter a sheet name', 'error');
                return;
            }}

            try {{
                const response = await fetch(`/admin/devices/${{deviceId}}`, {{
                    method: 'PUT',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        rink_id: rinkId,
                        sheet_name: sheetName,
                        device_name: deviceName || null
                    }})
                }});

                const result = await response.json();

                if (response.ok) {{
                    showMessage(`Device ${{deviceId}} saved successfully`, 'success');
                    setTimeout(() => location.reload(), 1500);
                }} else {{
                    showMessage(`Error: ${{result.detail || 'Failed to save'}}`, 'error');
                }}
            }} catch (error) {{
                showMessage(`Error: ${{error.message}}`, 'error');
            }}
        }}

        async function unassignDevice(deviceId) {{
            if (!confirm(`Unassign device ${{deviceId}}?`)) {{
                return;
            }}

            try {{
                const response = await fetch(`/admin/devices/${{deviceId}}/assignment`, {{
                    method: 'DELETE'
                }});

                const result = await response.json();

                if (response.ok) {{
                    showMessage(`Device ${{deviceId}} unassigned`, 'success');
                    setTimeout(() => location.reload(), 1500);
                }} else {{
                    showMessage(`Error: ${{result.detail || 'Failed to unassign'}}`, 'error');
                }}
            }} catch (error) {{
                showMessage(`Error: ${{error.message}}`, 'error');
            }}
        }}

        async function deleteDevice(deviceId) {{
            if (!confirm(`Delete device ${{deviceId}}? This will permanently remove it from the database.`)) {{
                return;
            }}

            try {{
                const response = await fetch(`/admin/devices/${{deviceId}}`, {{
                    method: 'DELETE'
                }});

                const result = await response.json();

                if (response.ok) {{
                    showMessage(`Device ${{deviceId}} deleted`, 'success');
                    setTimeout(() => location.reload(), 1500);
                }} else {{
                    showMessage(`Error: ${{result.detail || 'Failed to delete'}}`, 'error');
                }}
            }} catch (error) {{
                showMessage(`Error: ${{error.message}}`, 'error');
            }}
        }}

        async function saveRink(rinkId) {{
            const row = document.querySelector(`tr[data-rink-id="${{rinkId}}"]`);
            const nameInput = row.querySelector('.rink-name-input');
            const newName = nameInput.value.trim();

            if (!newName) {{
                showMessage('Rink name cannot be empty', 'error');
                return;
            }}

            try {{
                const response = await fetch(`/admin/rinks/${{rinkId}}`, {{
                    method: 'PUT',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        name: newName
                    }})
                }});

                const result = await response.json();

                if (response.ok) {{
                    showMessage(`Rink ${{rinkId}} updated successfully`, 'success');
                }} else {{
                    showMessage(`Error: ${{result.detail || 'Failed to update'}}`, 'error');
                }}
            }} catch (error) {{
                showMessage(`Error: ${{error.message}}`, 'error');
            }}
        }}

        function filterRinksTable() {{
            const filters = {{
                rinkId: document.getElementById('filterRinkId').value.toLowerCase(),
                rinkName: document.getElementById('filterRinkName').value.toLowerCase()
            }};

            const tbody = document.getElementById('rinksTableBody');
            const rows = tbody.getElementsByTagName('tr');

            for (let i = 0; i < rows.length; i++) {{
                const cells = rows[i].getElementsByTagName('td');
                if (cells.length < 2) continue;

                const rinkId = cells[0].textContent.toLowerCase();
                const rinkNameInput = cells[1].querySelector('input');
                const rinkName = rinkNameInput ? rinkNameInput.value.toLowerCase() : '';

                const match =
                    rinkId.includes(filters.rinkId) &&
                    rinkName.includes(filters.rinkName);

                rows[i].style.display = match ? '' : 'none';
            }}
        }}

        async function deleteRink(rinkId) {{
            if (!confirm(`Delete rink ${{rinkId}}? This will fail if devices are assigned to it.`)) {{
                return;
            }}

            try {{
                const response = await fetch(`/admin/rinks/${{rinkId}}`, {{
                    method: 'DELETE'
                }});

                const result = await response.json();

                if (response.ok) {{
                    showMessage(`Rink ${{rinkId}} deleted`, 'success');
                    setTimeout(() => location.reload(), 1500);
                }} else {{
                    showMessage(`Error: ${{result.detail || 'Failed to delete rink'}}`, 'error');
                }}
            }} catch (error) {{
                showMessage(`Error: ${{error.message}}`, 'error');
            }}
        }}

        async function addRink() {{
            const rinkId = document.getElementById('addRinkId').value.trim();
            const rinkName = document.getElementById('addRinkName').value.trim();

            if (!rinkId || !rinkName) {{
                showMessage('Rink ID and Name are required', 'error');
                return;
            }}

            try {{
                const response = await fetch('/admin/rinks', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        rink_id: rinkId,
                        name: rinkName
                    }})
                }});

                const result = await response.json();

                if (response.ok) {{
                    showMessage(`Rink ${{rinkId}} added successfully`, 'success');
                    document.getElementById('addRinkId').value = '';
                    document.getElementById('addRinkName').value = '';
                    setTimeout(() => location.reload(), 1500);
                }} else {{
                    showMessage(`Error: ${{result.detail || 'Failed to add rink'}}`, 'error');
                }}
            }} catch (error) {{
                showMessage(`Error: ${{error.message}}`, 'error');
            }}
        }}
        </script>
    </body>
    </html>
    """

    return HTMLResponse(content=html)


@app.post("/admin/devices")
async def create_device(request: CreateDeviceRequest):
    """
    Manually register a new device.

    This allows pre-registering devices before they connect.
    If rink_id and sheet_name are provided, the device will be marked as assigned.
    """
    logger.info(f"Creating device {request.device_id}")

    db = get_db()
    current_time = int(time.time())

    # Check if device already exists
    existing = db.execute(
        "SELECT device_id FROM devices WHERE device_id = ?",
        (request.device_id,)
    ).fetchone()

    if existing:
        db.close()
        raise HTTPException(
            status_code=409,
            detail=f"Device {request.device_id} already exists. Use PUT to update it."
        )

    # Validate rink_id if provided
    if request.rink_id:
        rink = db.execute("SELECT rink_id FROM rinks WHERE rink_id = ?", (request.rink_id,)).fetchone()
        if not rink:
            db.close()
            raise HTTPException(status_code=404, detail=f"Rink {request.rink_id} not found")

    # Determine if device should be marked as assigned
    is_assigned = 1 if (request.rink_id and request.sheet_name) else 0

    # Insert device
    db.execute("""
        INSERT INTO devices (
            device_id, rink_id, sheet_name, device_name, is_assigned,
            first_seen_at, last_seen_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        request.device_id,
        request.rink_id,
        request.sheet_name,
        request.device_name,
        is_assigned,
        current_time,
        current_time,
        request.notes
    ))

    db.commit()

    # Fetch created device
    created = db.execute("""
        SELECT device_id, rink_id, sheet_name, device_name, is_assigned,
               first_seen_at, last_seen_at, notes
        FROM devices
        WHERE device_id = ?
    """, (request.device_id,)).fetchone()

    db.close()

    logger.info(f"Successfully created device {request.device_id}")

    return {
        "status": "ok",
        "message": f"Device {request.device_id} created",
        "device": DeviceInfo(
            device_id=created["device_id"],
            rink_id=created["rink_id"],
            sheet_name=created["sheet_name"],
            device_name=created["device_name"],
            is_assigned=bool(created["is_assigned"]),
            first_seen_at=created["first_seen_at"],
            last_seen_at=created["last_seen_at"],
            notes=created["notes"]
        )
    }


@app.get("/admin/devices/{device_id}", response_model=DeviceInfo)
async def get_device(device_id: str):
    """Get details for a specific device."""
    db = get_db()

    device = db.execute("""
        SELECT device_id, rink_id, sheet_name, device_name, is_assigned,
               first_seen_at, last_seen_at, notes
        FROM devices
        WHERE device_id = ?
    """, (device_id,)).fetchone()

    db.close()

    if not device:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found")

    return DeviceInfo(
        device_id=device["device_id"],
        rink_id=device["rink_id"],
        sheet_name=device["sheet_name"],
        device_name=device["device_name"],
        is_assigned=bool(device["is_assigned"]),
        first_seen_at=device["first_seen_at"],
        last_seen_at=device["last_seen_at"],
        notes=device["notes"]
    )


@app.put("/admin/devices/{device_id}")
async def update_device(device_id: str, request: UpdateDeviceRequest):
    """
    Update a device's assignment and details.

    To assign an unassigned device, provide rink_id and sheet_name.
    To update an existing assignment, provide any fields you want to change.
    To unassign, use DELETE /admin/devices/{device_id}/assignment instead.
    """
    logger.info(f"Updating device {device_id}")

    db = get_db()

    # Check if device exists
    device = db.execute(
        "SELECT device_id FROM devices WHERE device_id = ?",
        (device_id,)
    ).fetchone()

    if not device:
        db.close()
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found. Device must connect at least once before assignment.")

    # Build update query dynamically based on provided fields
    updates = []
    params = []

    if request.rink_id is not None:
        # Validate rink exists
        rink = db.execute("SELECT rink_id FROM rinks WHERE rink_id = ?", (request.rink_id,)).fetchone()
        if not rink:
            db.close()
            raise HTTPException(status_code=404, detail=f"Rink {request.rink_id} not found")
        updates.append("rink_id = ?")
        params.append(request.rink_id)

    if request.sheet_name is not None:
        updates.append("sheet_name = ?")
        params.append(request.sheet_name)

    if request.device_name is not None:
        updates.append("device_name = ?")
        params.append(request.device_name)

    if request.notes is not None:
        updates.append("notes = ?")
        params.append(request.notes)

    # If rink_id and sheet_name are both provided, mark as assigned
    if request.rink_id is not None and request.sheet_name is not None:
        updates.append("is_assigned = 1")

    if not updates:
        db.close()
        return {"status": "ok", "message": "No changes requested"}

    # Execute update
    params.append(device_id)
    query = f"UPDATE devices SET {', '.join(updates)} WHERE device_id = ?"
    db.execute(query, params)
    db.commit()

    # Fetch updated device
    updated = db.execute("""
        SELECT device_id, rink_id, sheet_name, device_name, is_assigned,
               first_seen_at, last_seen_at, notes
        FROM devices
        WHERE device_id = ?
    """, (device_id,)).fetchone()

    db.close()

    logger.info(f"Successfully updated device {device_id}")

    return {
        "status": "ok",
        "message": f"Device {device_id} updated",
        "device": DeviceInfo(
            device_id=updated["device_id"],
            rink_id=updated["rink_id"],
            sheet_name=updated["sheet_name"],
            device_name=updated["device_name"],
            is_assigned=bool(updated["is_assigned"]),
            first_seen_at=updated["first_seen_at"],
            last_seen_at=updated["last_seen_at"],
            notes=updated["notes"]
        )
    }


@app.delete("/admin/devices/{device_id}/assignment")
async def unassign_device(device_id: str):
    """Clear a device's assignment (unassign from rink and sheet)."""
    logger.info(f"Unassigning device {device_id}")

    db = get_db()

    # Check if device exists
    device = db.execute(
        "SELECT device_id FROM devices WHERE device_id = ?",
        (device_id,)
    ).fetchone()

    if not device:
        db.close()
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found")

    # Unassign the device
    db.execute("""
        UPDATE devices
        SET rink_id = NULL,
            sheet_name = NULL,
            is_assigned = 0
        WHERE device_id = ?
    """, (device_id,))

    db.commit()
    db.close()

    logger.info(f"Successfully unassigned device {device_id}")

    return {
        "status": "ok",
        "message": f"Device {device_id} unassigned"
    }


@app.delete("/admin/devices/{device_id}")
async def delete_device(device_id: str):
    """Completely delete a device from the database."""
    logger.info(f"Deleting device {device_id}")

    db = get_db()

    # Check if device exists
    device = db.execute(
        "SELECT device_id FROM devices WHERE device_id = ?",
        (device_id,)
    ).fetchone()

    if not device:
        db.close()
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found")

    # Delete the device (this will cascade delete deliveries if we had FK constraints)
    db.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))

    db.commit()
    db.close()

    logger.info(f"Successfully deleted device {device_id}")

    return {
        "status": "ok",
        "message": f"Device {device_id} deleted"
    }


# Keep legacy endpoints for backwards compatibility
@app.post("/admin/devices/{device_id}/assign")
async def assign_device_legacy(device_id: str, request: AssignDeviceRequest):
    """Legacy endpoint - use PUT /admin/devices/{device_id} instead."""
    return await update_device(device_id, UpdateDeviceRequest(
        rink_id=request.rink_id,
        sheet_name=request.sheet_name,
        device_name=request.device_name,
        notes=request.notes
    ))


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
        "home_score": result.get("home_score", 0),
        "away_score": result.get("away_score", 0),
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
        <title>score-cloud | Games</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }
            .nav {
                background: rgba(255, 255, 255, 0.95);
                padding: 15px 30px;
                border-radius: 10px;
                margin-bottom: 20px;
                box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
            }
            .nav a {
                color: #667eea;
                text-decoration: none;
                margin-right: 20px;
                font-weight: 500;
            }
            .nav a:hover {
                text-decoration: underline;
            }
            .nav a.active {
                color: #764ba2;
                font-weight: 700;
            }
            .container {
                max-width: 1200px;
                margin: 0 auto;
                background: white;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
                overflow: hidden;
            }
            h1 {
                padding: 30px;
                margin: 0;
                font-size: 1.8em;
                color: #333;
                background: white;
                border-bottom: 1px solid #e9ecef;
            }
            .content {
                padding: 30px;
                background: white;
            }
            table {
                width: 100%;
                border-collapse: collapse;
            }
            th {
                background: #f8f9fa;
                padding: 12px 15px;
                text-align: left;
                font-weight: 600;
                color: #495057;
                border-bottom: 2px solid #dee2e6;
                font-size: 0.9em;
            }
            td {
                padding: 12px 15px;
                border-bottom: 1px solid #e9ecef;
                color: #495057;
            }
            tr:hover {
                background: #f8f9fa;
            }
            .game-id {
                font-family: 'Courier New', monospace;
                color: #667eea;
                font-size: 0.85em;
            }
            .clock {
                font-family: 'Courier New', monospace;
                font-weight: 600;
                font-size: 1.1em;
            }
            .status {
                font-size: 0.85em;
            }
            .status.running { color: #28a745; }
            .status.paused { color: #6c757d; }
            .no-games {
                text-align: center;
                padding: 60px 20px;
                color: #6c757d;
            }
            .filter-row input {
                width: 100%;
                padding: 6px 8px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                font-size: 0.85em;
            }
            .filter-row input:focus {
                outline: none;
                border-color: #667eea;
                box-shadow: 0 0 0 2px rgba(102, 126, 234, 0.1);
            }
            .filter-row td {
                padding: 8px 15px;
                background: #f8f9fa;
            }
            .nhl-loader {
                margin-bottom: 30px;
                padding: 20px;
                background: #f8f9fa;
                border-radius: 8px;
            }
            .nhl-loader h3 {
                margin-bottom: 15px;
                color: #495057;
                font-size: 1.1em;
            }
            .nhl-form {
                display: flex;
                gap: 10px;
                align-items: center;
            }
            .nhl-form input[type="date"] {
                padding: 8px 12px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                font-size: 0.9em;
            }
            .nhl-form button {
                padding: 8px 16px;
                background: #667eea;
                color: white;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 0.9em;
            }
            .nhl-form button:hover {
                background: #5568d3;
            }
            .nhl-message {
                margin-top: 10px;
                padding: 8px 12px;
                border-radius: 4px;
                font-size: 0.9em;
                display: none;
            }
            .nhl-message.success {
                background: #d4edda;
                color: #155724;
                display: block;
            }
            .nhl-message.error {
                background: #f8d7da;
                color: #721c24;
                display: block;
            }
        </style>
    </head>
    <body>
        <div class="nav">
            <a href="/admin/devices">Devices</a>
            <a href="/admin/games/state" class="active">Games</a>
            <a href="/admin/teams">Teams</a>
            <a href="/admin/players">Players</a>
            <a href="/admin/rosters">Rosters</a>
        </div>
        <div class="container">
            <h1>Games</h1>
            <div class="content">
                <div class="nhl-loader">
                    <h3>Load NHL Schedule</h3>
                    <div class="nhl-form">
                        <input type="date" id="nhlStartDate" placeholder="Start Date">
                        <input type="date" id="nhlEndDate" placeholder="End Date">
                        <button onclick="loadNHLSchedule()">Load Games</button>
                    </div>
                    <div id="nhlMessage" class="nhl-message"></div>
                </div>

                <table id="gamesTable">
                    <thead>
                        <tr>
                            <th style="width: 12%;">Game ID</th>
                            <th style="width: 12%;">Game Date</th>
                            <th style="width: 26%;">Teams</th>
                            <th style="width: 10%;">Score</th>
                            <th style="width: 12%;">Clock</th>
                            <th style="width: 10%;">Status</th>
                            <th style="width: 13%;">Period Length</th>
                        </tr>
                        <tr class="filter-row">
                            <td><input type="text" id="filterGameId" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterGameDate" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterTeams" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterScore" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterClock" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterStatus" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterPeriod" placeholder="Filter..." onkeyup="filterTable()"></td>
                        </tr>
                    </thead>
                    <tbody id="gamesBody">
                        <tr>
                            <td colspan="7" class="no-games">Loading...</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <script>
        function formatClock(seconds) {
            const mins = Math.floor(seconds / 60);
            const secs = seconds % 60;
            return `${mins}:${secs.toString().padStart(2, '0')}`;
        }

        function filterTable() {
            const filters = {
                gameId: document.getElementById('filterGameId').value.toLowerCase(),
                gameDate: document.getElementById('filterGameDate').value.toLowerCase(),
                teams: document.getElementById('filterTeams').value.toLowerCase(),
                score: document.getElementById('filterScore').value.toLowerCase(),
                clock: document.getElementById('filterClock').value.toLowerCase(),
                status: document.getElementById('filterStatus').value.toLowerCase(),
                period: document.getElementById('filterPeriod').value.toLowerCase()
            };

            const tbody = document.getElementById('gamesBody');
            const rows = tbody.getElementsByTagName('tr');

            for (let i = 0; i < rows.length; i++) {
                const cells = rows[i].getElementsByTagName('td');
                if (cells.length < 7) continue; // Skip "no games" row

                const gameId = cells[0].textContent.toLowerCase();
                const gameDate = cells[1].textContent.toLowerCase();
                const teams = cells[2].textContent.toLowerCase();
                const score = cells[3].textContent.toLowerCase();
                const clock = cells[4].textContent.toLowerCase();
                const status = cells[5].textContent.toLowerCase();
                const period = cells[6].textContent.toLowerCase();

                const match =
                    gameId.includes(filters.gameId) &&
                    gameDate.includes(filters.gameDate) &&
                    teams.includes(filters.teams) &&
                    score.includes(filters.score) &&
                    clock.includes(filters.clock) &&
                    status.includes(filters.status) &&
                    period.includes(filters.period);

                rows[i].style.display = match ? '' : 'none';
            }
        }

        function updateGameStates() {
            fetch('/admin/games/state?format=json')
                .then(response => response.json())
                .then(data => {
                    const tbody = document.getElementById('gamesBody');

                    if (data.games.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="7" class="no-games">No games found</td></tr>';
                        return;
                    }

                    let html = '';
                    data.games.forEach(game => {
                        const status = game.clock_running ? 'running' : 'paused';
                        const statusText = game.clock_running ? 'Running' : 'Paused';
                        const clock = formatClock(game.clock_seconds);
                        const score = `${game.home_score} - ${game.away_score}`;
                        // Convert UTC timestamp to local date (handles timezone offset)
                        const startTime = new Date(game.start_time);
                        const gameDate = startTime.toLocaleDateString('en-CA'); // YYYY-MM-DD format

                        html += `
                            <tr>
                                <td class="game-id">${game.game_id}</td>
                                <td>${gameDate}</td>
                                <td>${game.home_team} vs ${game.away_team}</td>
                                <td><strong>${score}</strong></td>
                                <td class="clock">${clock}</td>
                                <td><span class="status ${status}">${statusText}</span></td>
                                <td>${game.period_length_min} min</td>
                            </tr>
                        `;
                    });

                    tbody.innerHTML = html;
                })
                .catch(error => {
                    console.error('Error fetching game states:', error);
                });
        }

        async function loadNHLSchedule() {
            const startDate = document.getElementById('nhlStartDate').value;
            const endDate = document.getElementById('nhlEndDate').value;
            const messageDiv = document.getElementById('nhlMessage');

            if (!startDate) {
                messageDiv.textContent = 'Please select at least a start date';
                messageDiv.className = 'nhl-message error';
                return;
            }

            // Show loading message
            messageDiv.textContent = 'Loading NHL schedule...';
            messageDiv.className = 'nhl-message';
            messageDiv.style.display = 'block';

            try {
                let url = `/admin/load-nhl-schedule?start_date=${startDate}`;
                if (endDate) {
                    url += `&end_date=${endDate}`;
                }

                const response = await fetch(url, { method: 'POST' });
                const result = await response.json();

                if (result.status === 'ok') {
                    messageDiv.textContent = result.message;
                    messageDiv.className = 'nhl-message success';

                    // Reload game states after 1 second
                    setTimeout(() => {
                        updateGameStates();
                    }, 1000);
                } else {
                    messageDiv.textContent = result.message;
                    messageDiv.className = 'nhl-message error';
                }
            } catch (error) {
                messageDiv.textContent = `Error: ${error.message}`;
                messageDiv.className = 'nhl-message error';
            }
        }

        // Load game states on page load
        updateGameStates();

        // Set default dates to today
        const today = new Date().toISOString().split('T')[0];
        document.getElementById('nhlStartDate').value = today;
        document.getElementById('nhlEndDate').value = today;
        </script>
    </body>
    </html>
    """

    return HTMLResponse(content=html)


@app.get("/admin/rosters")
async def get_rosters_admin(format: Optional[str] = Query(None, description="Response format: 'json' or 'html'")):
    """
    Admin page to view all team rosters.

    Returns HTML for browser viewing or JSON if format=json parameter is provided.
    """
    from fastapi.responses import HTMLResponse

    db = get_db()

    # Get all roster entries with player details
    # Join with teams table to get team info
    rosters = db.execute("""
        SELECT DISTINCT
            tr.team_abbrev,
            t.city,
            t.team_name,
            t.full_name AS team_full_name,
            p.player_id,
            p.full_name,
            p.jersey_number,
            p.position,
            tr.roster_status,
            tr.added_at,
            tr.removed_at
        FROM team_rosters tr
        JOIN players p ON tr.player_id = p.player_id
        LEFT JOIN teams t ON tr.team_abbrev = t.team_abbrev
        ORDER BY tr.team_abbrev, p.position, p.jersey_number
    """).fetchall()

    db.close()

    # Return JSON if requested
    if format == "json":
        return {"rosters": [dict(r) for r in rosters]}

    # Generate HTML view
    import datetime

    def format_timestamp(ts):
        if ts:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        return "Active"

    rosters_html = ""
    if not rosters:
        rosters_html = '<tr><td colspan="9" style="text-align: center; color: #999; padding: 40px;">No rosters found. Click "Load Rosters" to fetch from NHL API.</td></tr>'
    else:
        for r in rosters:
            status_class = "active" if r["roster_status"] == "active" else "inactive"
            removed_display = "Active" if r["removed_at"] is None else format_timestamp(r["removed_at"])

            # Use team info from teams table
            team_city = r["city"] or "-"
            team_name = r["team_name"] or "-"

            rosters_html += f'''
            <tr>
                <td class="team-abbrev">{r["team_abbrev"]}</td>
                <td>{team_city}</td>
                <td>{team_name}</td>
                <td>{r["jersey_number"] or "-"}</td>
                <td>{r["full_name"]}</td>
                <td>{r["position"]}</td>
                <td><span class="status-badge {status_class}">{r["roster_status"]}</span></td>
                <td class="timestamp">{format_timestamp(r["added_at"])}</td>
                <td class="timestamp">{removed_display}</td>
            </tr>
            '''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Rosters</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }}
            .nav {{
                background: rgba(255, 255, 255, 0.95);
                padding: 15px 30px;
                border-radius: 10px;
                margin-bottom: 20px;
                box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
            }}
            .nav a {{
                color: #667eea;
                text-decoration: none;
                margin-right: 20px;
                font-weight: 500;
            }}
            .nav a:hover {{
                text-decoration: underline;
            }}
            .nav a.active {{
                color: #764ba2;
                font-weight: 700;
            }}
            .container {{
                max-width: 1400px;
                margin: 0 auto;
                background: white;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
                overflow: hidden;
            }}
            h1 {{
                padding: 30px;
                margin: 0;
                font-size: 1.8em;
                color: #333;
                background: white;
                border-bottom: 1px solid #e9ecef;
            }}
            .content {{
                padding: 30px;
                background: white;
            }}
            .roster-loader {{
                margin-bottom: 30px;
                padding: 20px;
                background: #f8f9fa;
                border-radius: 8px;
            }}
            .roster-loader h3 {{
                margin-bottom: 10px;
                font-size: 1em;
                color: #495057;
            }}
            .roster-loader button {{
                padding: 10px 20px;
                background: #667eea;
                color: white;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 0.95em;
                font-weight: 500;
            }}
            .roster-loader button:hover {{
                background: #5568d3;
            }}
            .roster-message {{
                margin-top: 15px;
                padding: 12px;
                border-radius: 4px;
                display: none;
            }}
            .roster-message.success {{
                background: #d4edda;
                color: #155724;
                display: block;
            }}
            .roster-message.error {{
                background: #f8d7da;
                color: #721c24;
                display: block;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
            }}
            th {{
                background: #f8f9fa;
                padding: 12px 15px;
                text-align: left;
                font-weight: 600;
                color: #495057;
                border-bottom: 2px solid #dee2e6;
                font-size: 0.9em;
            }}
            td {{
                padding: 12px 15px;
                border-bottom: 1px solid #e9ecef;
            }}
            tr:hover {{
                background: #f8f9fa;
            }}
            .team-abbrev {{
                font-family: 'Courier New', monospace;
                font-weight: 600;
                color: #667eea;
            }}
            .status-badge {{
                padding: 4px 10px;
                border-radius: 4px;
                font-size: 0.85em;
                font-weight: 500;
            }}
            .status-badge.active {{
                background: #d4edda;
                color: #155724;
            }}
            .status-badge.inactive {{
                background: #f8d7da;
                color: #721c24;
            }}
            .timestamp {{
                color: #6c757d;
                font-size: 0.9em;
            }}
            .filter-row input {{
                width: 100%;
                padding: 6px 8px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                font-size: 0.85em;
            }}
            .filter-row input:focus {{
                outline: none;
                border-color: #667eea;
                box-shadow: 0 0 0 2px rgba(102, 126, 234, 0.1);
            }}
            .filter-row td {{
                padding: 8px 15px;
                background: #f8f9fa;
            }}
        </style>
        <script>
            function loadRosters() {{
                const btn = document.querySelector('.roster-loader button');
                const msg = document.getElementById('rosterMessage');

                btn.disabled = true;
                btn.textContent = 'Loading...';
                msg.className = 'roster-message';
                msg.textContent = '';

                // Get unique teams from current games
                fetch('/admin/load-rosters', {{ method: 'POST' }})
                    .then(response => response.json())
                    .then(data => {{
                        if (data.status === 'ok') {{
                            msg.className = 'roster-message success';
                            msg.textContent = data.message;
                            setTimeout(() => location.reload(), 1000);
                        }} else {{
                            msg.className = 'roster-message error';
                            msg.textContent = data.message || 'Failed to load rosters';
                            btn.disabled = false;
                            btn.textContent = 'Load Rosters';
                        }}
                    }})
                    .catch(error => {{
                        msg.className = 'roster-message error';
                        msg.textContent = 'Error: ' + error.message;
                        btn.disabled = false;
                        btn.textContent = 'Load Rosters';
                    }});
            }}

            function filterTable() {{
                const filters = {{
                    team: document.getElementById('filterTeam').value.toLowerCase(),
                    city: document.getElementById('filterCity').value.toLowerCase(),
                    teamName: document.getElementById('filterTeamName').value.toLowerCase(),
                    number: document.getElementById('filterNumber').value.toLowerCase(),
                    name: document.getElementById('filterName').value.toLowerCase(),
                    position: document.getElementById('filterPosition').value.toLowerCase(),
                    status: document.getElementById('filterStatus').value.toLowerCase()
                }};

                const rows = document.querySelectorAll('#rostersTable tbody tr');

                rows.forEach(row => {{
                    if (row.cells.length < 7) return; // Skip empty row

                    const team = row.cells[0].textContent.toLowerCase();
                    const city = row.cells[1].textContent.toLowerCase();
                    const teamName = row.cells[2].textContent.toLowerCase();
                    const number = row.cells[3].textContent.toLowerCase();
                    const name = row.cells[4].textContent.toLowerCase();
                    const position = row.cells[5].textContent.toLowerCase();
                    const status = row.cells[6].textContent.toLowerCase();

                    const match =
                        team.includes(filters.team) &&
                        city.includes(filters.city) &&
                        teamName.includes(filters.teamName) &&
                        number.includes(filters.number) &&
                        name.includes(filters.name) &&
                        position.includes(filters.position) &&
                        status.includes(filters.status);

                    row.style.display = match ? '' : 'none';
                }});
            }}
        </script>
    </head>
    <body>
        <div class="nav">
            <a href="/admin/devices">Devices</a>
            <a href="/admin/games/state">Games</a>
            <a href="/admin/teams">Teams</a>
            <a href="/admin/players">Players</a>
            <a href="/admin/rosters" class="active">Rosters</a>
        </div>
        <div class="container">
            <h1>Team Rosters</h1>
            <div class="content">
                <div class="roster-loader">
                    <h3>Load Team Rosters from NHL API</h3>
                    <button onclick="loadRosters()">Load Rosters</button>
                    <div id="rosterMessage" class="roster-message"></div>
                </div>

                <table id="rostersTable">
                    <thead>
                        <tr>
                            <th>Abbrev</th>
                            <th>City</th>
                            <th>Team Name</th>
                            <th>#</th>
                            <th>Player Name</th>
                            <th>Position</th>
                            <th>Status</th>
                            <th>Added</th>
                            <th>Removed</th>
                        </tr>
                        <tr class="filter-row">
                            <td><input type="text" id="filterTeam" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterCity" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterTeamName" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterNumber" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterName" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterPosition" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterStatus" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td></td>
                            <td></td>
                        </tr>
                    </thead>
                    <tbody>
                        {rosters_html}
                    </tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    '''

    return HTMLResponse(content=html)


@app.post("/admin/load-rosters")
async def load_rosters():
    """
    Load rosters for all NHL teams.

    Fetches the list of all NHL teams and loads their current rosters.
    """
    # List of all NHL team abbreviations (2024-25 season)
    nhl_teams = [
        "ANA", "BOS", "BUF", "CAR", "CBJ", "CGY", "CHI", "COL", "DAL", "DET",
        "EDM", "FLA", "LAK", "MIN", "MTL", "NJD", "NSH", "NYI", "NYR", "OTT",
        "PHI", "PIT", "SEA", "SJS", "STL", "TBL", "TOR", "UTA", "VAN", "VGK",
        "WPG", "WSH"
    ]

    db = get_db()
    teams_loaded = 0
    teams_failed = 0

    for team_abbrev in nhl_teams:
        logger.info(f"Fetching roster for {team_abbrev}...")

        # Fetch roster from NHL API
        roster = fetch_nhl_roster(team_abbrev)

        if roster:
            store_roster_in_db(team_abbrev, roster, db)
            teams_loaded += 1
        else:
            teams_failed += 1
            logger.warning(f"Failed to load roster for {team_abbrev}")

    db.commit()
    db.close()

    message = f"Loaded rosters for {teams_loaded} teams"
    if teams_failed > 0:
        message += f" ({teams_failed} failed)"

    return {
        "status": "ok",
        "message": message,
        "teams_loaded": teams_loaded,
        "teams_failed": teams_failed
    }


@app.post("/admin/load-teams")
async def load_teams_endpoint():
    """
    Load NHL teams into the teams table.

    Populates the teams table with all 32 NHL teams.
    """
    logger.info("Loading NHL teams...")
    result = load_nhl_teams()
    return result


@app.get("/admin/teams")
async def get_teams_admin(format: Optional[str] = Query(None, description="Response format: 'json' or 'html'")):
    """
    Admin page to view all NHL teams.

    Returns HTML for browser viewing or JSON if format=json parameter is provided.
    """
    from fastapi.responses import HTMLResponse

    db = get_db()

    teams = db.execute("""
        SELECT team_abbrev, city, team_name, full_name, conference, division, created_at
        FROM teams
        ORDER BY full_name
    """).fetchall()

    db.close()

    # Return JSON if requested
    if format == "json":
        return {"teams": [dict(t) for t in teams]}

    # Generate HTML view
    import datetime

    def format_timestamp(ts):
        if ts:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        return "Never"

    teams_html = ""
    if not teams:
        teams_html = '<tr><td colspan="7" style="text-align: center; color: #999; padding: 40px;">No teams found. Click "Load Teams" to populate.</td></tr>'
    else:
        for t in teams:
            teams_html += f'''
            <tr>
                <td class="team-abbrev">{t["team_abbrev"]}</td>
                <td>{t["city"]}</td>
                <td>{t["team_name"]}</td>
                <td>{t["full_name"]}</td>
                <td>{t["conference"]}</td>
                <td>{t["division"]}</td>
                <td class="timestamp">{format_timestamp(t["created_at"])}</td>
            </tr>
            '''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Teams</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }}
            .nav {{
                background: rgba(255, 255, 255, 0.95);
                padding: 15px 30px;
                border-radius: 10px;
                margin-bottom: 20px;
                box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
            }}
            .nav a {{
                color: #667eea;
                text-decoration: none;
                margin-right: 20px;
                font-weight: 500;
            }}
            .nav a:hover {{
                text-decoration: underline;
            }}
            .nav a.active {{
                color: #764ba2;
                font-weight: 700;
            }}
            .container {{
                max-width: 1400px;
                margin: 0 auto;
                background: white;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
                overflow: hidden;
            }}
            h1 {{
                padding: 30px;
                margin: 0;
                font-size: 1.8em;
                color: #333;
                background: white;
                border-bottom: 1px solid #e9ecef;
            }}
            .content {{
                padding: 30px;
                background: white;
            }}
            .teams-loader {{
                margin-bottom: 30px;
                padding: 20px;
                background: #f8f9fa;
                border-radius: 8px;
            }}
            .teams-loader h3 {{
                margin-bottom: 10px;
                font-size: 1em;
                color: #495057;
            }}
            .teams-loader button {{
                padding: 10px 20px;
                background: #667eea;
                color: white;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 0.95em;
                font-weight: 500;
            }}
            .teams-loader button:hover {{
                background: #5568d3;
            }}
            .teams-message {{
                margin-top: 15px;
                padding: 12px;
                border-radius: 4px;
                display: none;
            }}
            .teams-message.success {{
                background: #d4edda;
                color: #155724;
                display: block;
            }}
            .teams-message.error {{
                background: #f8d7da;
                color: #721c24;
                display: block;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
            }}
            th {{
                background: #f8f9fa;
                padding: 12px 15px;
                text-align: left;
                font-weight: 600;
                color: #495057;
                border-bottom: 2px solid #dee2e6;
                font-size: 0.9em;
            }}
            td {{
                padding: 12px 15px;
                border-bottom: 1px solid #e9ecef;
            }}
            tr:hover {{
                background: #f8f9fa;
            }}
            .team-abbrev {{
                font-family: 'Courier New', monospace;
                font-weight: 600;
                color: #667eea;
            }}
            .timestamp {{
                color: #6c757d;
                font-size: 0.9em;
            }}
            .filter-row input {{
                width: 100%;
                padding: 6px 8px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                font-size: 0.85em;
            }}
            .filter-row input:focus {{
                outline: none;
                border-color: #667eea;
                box-shadow: 0 0 0 2px rgba(102, 126, 234, 0.1);
            }}
            .filter-row td {{
                padding: 8px 15px;
                background: #f8f9fa;
            }}
        </style>
        <script>
            function loadTeams() {{
                const btn = document.querySelector('.teams-loader button');
                const msg = document.getElementById('teamsMessage');

                btn.disabled = true;
                btn.textContent = 'Loading...';
                msg.className = 'teams-message';
                msg.textContent = '';

                fetch('/admin/load-teams', {{ method: 'POST' }})
                    .then(response => response.json())
                    .then(data => {{
                        if (data.status === 'ok') {{
                            msg.className = 'teams-message success';
                            msg.textContent = `Loaded ${{data.teams_loaded}} NHL teams`;
                            setTimeout(() => location.reload(), 1000);
                        }} else {{
                            msg.className = 'teams-message error';
                            msg.textContent = data.message || 'Failed to load teams';
                            btn.disabled = false;
                            btn.textContent = 'Load Teams';
                        }}
                    }})
                    .catch(error => {{
                        msg.className = 'teams-message error';
                        msg.textContent = 'Error: ' + error.message;
                        btn.disabled = false;
                        btn.textContent = 'Load Teams';
                    }});
            }}

            function filterTable() {{
                const filters = {{
                    abbrev: document.getElementById('filterAbbrev').value.toLowerCase(),
                    city: document.getElementById('filterCity').value.toLowerCase(),
                    teamName: document.getElementById('filterTeamName').value.toLowerCase(),
                    fullName: document.getElementById('filterFullName').value.toLowerCase(),
                    conference: document.getElementById('filterConference').value.toLowerCase(),
                    division: document.getElementById('filterDivision').value.toLowerCase()
                }};

                const rows = document.querySelectorAll('#teamsTable tbody tr');

                rows.forEach(row => {{
                    if (row.cells.length < 6) return; // Skip empty row

                    const abbrev = row.cells[0].textContent.toLowerCase();
                    const city = row.cells[1].textContent.toLowerCase();
                    const teamName = row.cells[2].textContent.toLowerCase();
                    const fullName = row.cells[3].textContent.toLowerCase();
                    const conference = row.cells[4].textContent.toLowerCase();
                    const division = row.cells[5].textContent.toLowerCase();

                    const match =
                        abbrev.includes(filters.abbrev) &&
                        city.includes(filters.city) &&
                        teamName.includes(filters.teamName) &&
                        fullName.includes(filters.fullName) &&
                        conference.includes(filters.conference) &&
                        division.includes(filters.division);

                    row.style.display = match ? '' : 'none';
                }});
            }}
        </script>
    </head>
    <body>
        <div class="nav">
            <a href="/admin/devices">Devices</a>
            <a href="/admin/games/state">Games</a>
            <a href="/admin/teams" class="active">Teams</a>
            <a href="/admin/players">Players</a>
            <a href="/admin/rosters">Rosters</a>
        </div>
        <div class="container">
            <h1>NHL Teams</h1>
            <div class="content">
                <div class="teams-loader">
                    <h3>Load NHL Teams</h3>
                    <button onclick="loadTeams()">Load Teams</button>
                    <div id="teamsMessage" class="teams-message"></div>
                </div>

                <table id="teamsTable">
                    <thead>
                        <tr>
                            <th>Abbrev</th>
                            <th>City</th>
                            <th>Team Name</th>
                            <th>Full Name</th>
                            <th>Conference</th>
                            <th>Division</th>
                            <th>Created</th>
                        </tr>
                        <tr class="filter-row">
                            <td><input type="text" id="filterAbbrev" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterCity" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterTeamName" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterFullName" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterConference" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterDivision" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td></td>
                        </tr>
                    </thead>
                    <tbody>
                        {teams_html}
                    </tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    '''

    return HTMLResponse(content=html)


@app.get("/admin/players")
async def get_players_admin(format: Optional[str] = Query(None, description="Response format: 'json' or 'html'")):
    """
    Admin page to view all players.

    Returns HTML for browser viewing or JSON if format=json parameter is provided.
    """
    from fastapi.responses import HTMLResponse

    db = get_db()

    players = db.execute("""
        SELECT player_id, full_name, first_name, last_name,
               jersey_number, position, shoots_catches,
               height_inches, weight_pounds, birth_date,
               birth_city, birth_country, created_at
        FROM players
        ORDER BY last_name, first_name
    """).fetchall()

    db.close()

    # Return JSON if requested
    if format == "json":
        return {"players": [dict(p) for p in players]}

    # Generate HTML view
    import datetime

    def format_timestamp(ts):
        if ts:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        return "Never"

    players_html = ""
    if not players:
        players_html = '<tr><td colspan="13" style="text-align: center; color: #999; padding: 40px;">No players found. Load rosters to populate players.</td></tr>'
    else:
        for p in players:
            height_str = f'{p["height_inches"] // 12}\'{p["height_inches"] % 12}"' if p["height_inches"] else "-"
            weight_str = f'{p["weight_pounds"]} lbs' if p["weight_pounds"] else "-"

            players_html += f'''
            <tr>
                <td class="player-id">{p["player_id"]}</td>
                <td>{p["full_name"]}</td>
                <td>{p["first_name"] or "-"}</td>
                <td>{p["last_name"] or "-"}</td>
                <td>{p["jersey_number"] if p["jersey_number"] else "-"}</td>
                <td>{p["position"] or "-"}</td>
                <td>{p["shoots_catches"] or "-"}</td>
                <td>{height_str}</td>
                <td>{weight_str}</td>
                <td>{p["birth_date"] or "-"}</td>
                <td>{p["birth_city"] or "-"}</td>
                <td>{p["birth_country"] or "-"}</td>
                <td class="timestamp">{format_timestamp(p["created_at"])}</td>
            </tr>
            '''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Players</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }}
            .nav {{
                background: rgba(255, 255, 255, 0.95);
                padding: 15px 30px;
                border-radius: 10px;
                margin-bottom: 20px;
                box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
            }}
            .nav a {{
                color: #667eea;
                text-decoration: none;
                margin-right: 20px;
                font-weight: 500;
            }}
            .nav a:hover {{
                text-decoration: underline;
            }}
            .nav a.active {{
                color: #764ba2;
                font-weight: 700;
            }}
            .container {{
                max-width: 1600px;
                margin: 0 auto;
                background: white;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
                overflow: hidden;
            }}
            h1 {{
                padding: 30px;
                margin: 0;
                font-size: 1.8em;
                color: #333;
                background: white;
                border-bottom: 1px solid #e9ecef;
            }}
            .content {{
                padding: 30px;
                background: white;
                overflow-x: auto;
            }}
            .hint {{
                margin-bottom: 20px;
                color: #6c757d;
                font-size: 0.9em;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                min-width: 1200px;
            }}
            th {{
                background: #f8f9fa;
                padding: 12px 15px;
                text-align: left;
                font-weight: 600;
                color: #495057;
                border-bottom: 2px solid #dee2e6;
                font-size: 0.9em;
                white-space: nowrap;
            }}
            td {{
                padding: 12px 15px;
                border-bottom: 1px solid #e9ecef;
                white-space: nowrap;
            }}
            tr:hover {{
                background: #f8f9fa;
            }}
            .player-id {{
                font-family: 'Courier New', monospace;
                font-weight: 600;
                color: #667eea;
                font-size: 0.85em;
            }}
            .timestamp {{
                color: #6c757d;
                font-size: 0.9em;
            }}
            .filter-row input {{
                width: 100%;
                padding: 6px 8px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                font-size: 0.85em;
            }}
            .filter-row input:focus {{
                outline: none;
                border-color: #667eea;
                box-shadow: 0 0 0 2px rgba(102, 126, 234, 0.1);
            }}
            .filter-row td {{
                padding: 8px 15px;
                background: #f8f9fa;
            }}
        </style>
        <script>
            function filterTable() {{
                const filters = {{
                    playerId: document.getElementById('filterPlayerId').value.toLowerCase(),
                    fullName: document.getElementById('filterFullName').value.toLowerCase(),
                    firstName: document.getElementById('filterFirstName').value.toLowerCase(),
                    lastName: document.getElementById('filterLastName').value.toLowerCase(),
                    jersey: document.getElementById('filterJersey').value.toLowerCase(),
                    position: document.getElementById('filterPosition').value.toLowerCase(),
                    shoots: document.getElementById('filterShoots').value.toLowerCase(),
                    birthCity: document.getElementById('filterBirthCity').value.toLowerCase(),
                    birthCountry: document.getElementById('filterBirthCountry').value.toLowerCase()
                }};

                const rows = document.querySelectorAll('#playersTable tbody tr');

                rows.forEach(row => {{
                    if (row.cells.length < 13) return; // Skip empty row

                    const playerId = row.cells[0].textContent.toLowerCase();
                    const fullName = row.cells[1].textContent.toLowerCase();
                    const firstName = row.cells[2].textContent.toLowerCase();
                    const lastName = row.cells[3].textContent.toLowerCase();
                    const jersey = row.cells[4].textContent.toLowerCase();
                    const position = row.cells[5].textContent.toLowerCase();
                    const shoots = row.cells[6].textContent.toLowerCase();
                    const birthCity = row.cells[10].textContent.toLowerCase();
                    const birthCountry = row.cells[11].textContent.toLowerCase();

                    const match =
                        playerId.includes(filters.playerId) &&
                        fullName.includes(filters.fullName) &&
                        firstName.includes(filters.firstName) &&
                        lastName.includes(filters.lastName) &&
                        jersey.includes(filters.jersey) &&
                        position.includes(filters.position) &&
                        shoots.includes(filters.shoots) &&
                        birthCity.includes(filters.birthCity) &&
                        birthCountry.includes(filters.birthCountry);

                    row.style.display = match ? '' : 'none';
                }});
            }}
        </script>
    </head>
    <body>
        <div class="nav">
            <a href="/admin/devices">Devices</a>
            <a href="/admin/games/state">Games</a>
            <a href="/admin/teams">Teams</a>
            <a href="/admin/players" class="active">Players</a>
            <a href="/admin/rosters">Rosters</a>
        </div>
        <div class="container">
            <h1>NHL Players</h1>
            <div class="content">
                <div class="hint">
                    Players are automatically loaded when you load rosters. Use the Rosters page to load player data.
                </div>

                <table id="playersTable">
                    <thead>
                        <tr>
                            <th>Player ID</th>
                            <th>Full Name</th>
                            <th>First Name</th>
                            <th>Last Name</th>
                            <th>#</th>
                            <th>Pos</th>
                            <th>S/C</th>
                            <th>Height</th>
                            <th>Weight</th>
                            <th>Birth Date</th>
                            <th>Birth City</th>
                            <th>Birth Country</th>
                            <th>Created</th>
                        </tr>
                        <tr class="filter-row">
                            <td><input type="text" id="filterPlayerId" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterFullName" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterFirstName" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterLastName" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterJersey" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterPosition" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterShoots" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td></td>
                            <td></td>
                            <td></td>
                            <td><input type="text" id="filterBirthCity" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterBirthCountry" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td></td>
                        </tr>
                    </thead>
                    <tbody>
                        {players_html}
                    </tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    '''

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

def fetch_nhl_roster(team_abbreviation):
    """
    Fetch current roster from NHL API for a team.

    Args:
        team_abbreviation: Team code (e.g., "SEA", "TOR", "MTL")

    Returns:
        List of player dictionaries with NHL API data
    """
    try:
        url = f"https://api-web.nhle.com/v1/roster/{team_abbreviation}/current"
        logger.info(f"Fetching NHL roster from {url}")

        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        players = []
        current_time = int(time.time())

        # Parse forwards, defensemen, goalies
        for position_group in ["forwards", "defensemen", "goalies"]:
            for player in data.get(position_group, []):
                # Extract first and last names safely
                first_name = player.get("firstName", {})
                if isinstance(first_name, dict):
                    first_name = first_name.get("default", "")

                last_name = player.get("lastName", {})
                if isinstance(last_name, dict):
                    last_name = last_name.get("default", "")

                # Extract birth city safely
                birth_city = player.get("birthCity", {})
                if isinstance(birth_city, dict):
                    birth_city = birth_city.get("default")

                players.append({
                    "player_id": player["id"],
                    "full_name": f"{first_name} {last_name}".strip(),
                    "first_name": first_name,
                    "last_name": last_name,
                    "jersey_number": player.get("sweaterNumber"),
                    "position": player.get("positionCode"),
                    "shoots_catches": player.get("shootsCatches"),
                    "height_inches": player.get("heightInInches"),
                    "weight_pounds": player.get("weightInPounds"),
                    "birth_date": player.get("birthDate"),
                    "birth_city": birth_city,
                    "birth_country": player.get("birthCountry"),
                    "status": "active",
                    "created_at": current_time
                })

        logger.info(f"Fetched {len(players)} players for {team_abbreviation}")
        return players

    except Exception as e:
        logger.warning(f"Failed to fetch roster for {team_abbreviation}: {e}")
        return []


def load_nhl_teams():
    """
    Load all NHL teams into the teams table.

    Returns dict with status and count of teams loaded.
    """
    # All NHL teams for 2024-25 season with their info
    nhl_teams_data = [
        {"abbrev": "ANA", "city": "Anaheim", "name": "Ducks", "conference": "Western", "division": "Pacific"},
        {"abbrev": "BOS", "city": "Boston", "name": "Bruins", "conference": "Eastern", "division": "Atlantic"},
        {"abbrev": "BUF", "city": "Buffalo", "name": "Sabres", "conference": "Eastern", "division": "Atlantic"},
        {"abbrev": "CAR", "city": "Carolina", "name": "Hurricanes", "conference": "Eastern", "division": "Metropolitan"},
        {"abbrev": "CBJ", "city": "Columbus", "name": "Blue Jackets", "conference": "Eastern", "division": "Metropolitan"},
        {"abbrev": "CGY", "city": "Calgary", "name": "Flames", "conference": "Western", "division": "Pacific"},
        {"abbrev": "CHI", "city": "Chicago", "name": "Blackhawks", "conference": "Western", "division": "Central"},
        {"abbrev": "COL", "city": "Colorado", "name": "Avalanche", "conference": "Western", "division": "Central"},
        {"abbrev": "DAL", "city": "Dallas", "name": "Stars", "conference": "Western", "division": "Central"},
        {"abbrev": "DET", "city": "Detroit", "name": "Red Wings", "conference": "Eastern", "division": "Atlantic"},
        {"abbrev": "EDM", "city": "Edmonton", "name": "Oilers", "conference": "Western", "division": "Pacific"},
        {"abbrev": "FLA", "city": "Florida", "name": "Panthers", "conference": "Eastern", "division": "Atlantic"},
        {"abbrev": "LAK", "city": "Los Angeles", "name": "Kings", "conference": "Western", "division": "Pacific"},
        {"abbrev": "MIN", "city": "Minnesota", "name": "Wild", "conference": "Western", "division": "Central"},
        {"abbrev": "MTL", "city": "Montreal", "name": "Canadiens", "conference": "Eastern", "division": "Atlantic"},
        {"abbrev": "NJD", "city": "New Jersey", "name": "Devils", "conference": "Eastern", "division": "Metropolitan"},
        {"abbrev": "NSH", "city": "Nashville", "name": "Predators", "conference": "Western", "division": "Central"},
        {"abbrev": "NYI", "city": "New York", "name": "Islanders", "conference": "Eastern", "division": "Metropolitan"},
        {"abbrev": "NYR", "city": "New York", "name": "Rangers", "conference": "Eastern", "division": "Metropolitan"},
        {"abbrev": "OTT", "city": "Ottawa", "name": "Senators", "conference": "Eastern", "division": "Atlantic"},
        {"abbrev": "PHI", "city": "Philadelphia", "name": "Flyers", "conference": "Eastern", "division": "Metropolitan"},
        {"abbrev": "PIT", "city": "Pittsburgh", "name": "Penguins", "conference": "Eastern", "division": "Metropolitan"},
        {"abbrev": "SEA", "city": "Seattle", "name": "Kraken", "conference": "Western", "division": "Pacific"},
        {"abbrev": "SJS", "city": "San Jose", "name": "Sharks", "conference": "Western", "division": "Pacific"},
        {"abbrev": "STL", "city": "St. Louis", "name": "Blues", "conference": "Western", "division": "Central"},
        {"abbrev": "TBL", "city": "Tampa Bay", "name": "Lightning", "conference": "Eastern", "division": "Atlantic"},
        {"abbrev": "TOR", "city": "Toronto", "name": "Maple Leafs", "conference": "Eastern", "division": "Atlantic"},
        {"abbrev": "UTA", "city": "Utah", "name": "Hockey Club", "conference": "Western", "division": "Central"},
        {"abbrev": "VAN", "city": "Vancouver", "name": "Canucks", "conference": "Western", "division": "Pacific"},
        {"abbrev": "VGK", "city": "Vegas", "name": "Golden Knights", "conference": "Western", "division": "Pacific"},
        {"abbrev": "WPG", "city": "Winnipeg", "name": "Jets", "conference": "Western", "division": "Central"},
        {"abbrev": "WSH", "city": "Washington", "name": "Capitals", "conference": "Eastern", "division": "Metropolitan"},
    ]

    db = get_db()
    current_time = int(time.time())
    teams_loaded = 0

    for team in nhl_teams_data:
        full_name = f"{team['city']} {team['name']}"
        db.execute("""
            INSERT OR REPLACE INTO teams (
                team_abbrev, city, team_name, full_name, conference, division, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            team["abbrev"],
            team["city"],
            team["name"],
            full_name,
            team["conference"],
            team["division"],
            current_time
        ))
        teams_loaded += 1

    db.commit()
    db.close()

    logger.info(f"Loaded {teams_loaded} NHL teams")
    return {
        "status": "ok",
        "teams_loaded": teams_loaded
    }


def store_roster_in_db(team_abbrev, players, db):
    """
    Store roster players in database for a team.

    Uses temporal tracking - checks if roster already exists for this team
    at this time to avoid duplicates.

    Args:
        team_abbrev: Team abbreviation (e.g., "SEA", "TOR")
        players: List of player dicts from NHL API
        db: Database connection
    """
    current_time = int(time.time())

    for player in players:
        player_id = player["player_id"]

        # Insert or update player in players table
        db.execute("""
            INSERT OR REPLACE INTO players (
                player_id, full_name, first_name, last_name,
                jersey_number, position, shoots_catches,
                height_inches, weight_pounds, birth_date,
                birth_city, birth_country, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            player_id,
            player["full_name"],
            player["first_name"],
            player["last_name"],
            player.get("jersey_number"),
            player.get("position"),
            player.get("shoots_catches"),
            player.get("height_inches"),
            player.get("weight_pounds"),
            player.get("birth_date"),
            player.get("birth_city"),
            player.get("birth_country"),
            player.get("created_at", current_time)
        ))

        # Check if player already exists on this team's roster (active roster entry)
        existing = db.execute("""
            SELECT id FROM team_rosters
            WHERE player_id = ? AND team_abbrev = ? AND removed_at IS NULL
        """, (player_id, team_abbrev)).fetchone()

        if not existing:
            # Add player to team roster with temporal tracking
            db.execute("""
                INSERT INTO team_rosters (
                    player_id, team_abbrev, roster_status, added_at, removed_at
                ) VALUES (?, ?, ?, ?, NULL)
            """, (
                player_id,
                team_abbrev,
                player.get("status", "active"),
                current_time
            ))
            logger.debug(f"Added player {player_id} to {team_abbrev} roster")
        else:
            # Update status if changed
            db.execute("""
                UPDATE team_rosters
                SET roster_status = ?
                WHERE id = ?
            """, (player.get("status", "active"), existing["id"]))

    logger.info(f"Stored {len(players)} players for team {team_abbrev}")


def fetch_nhl_schedule(start_date=None, end_date=None):
    """
    Fetch NHL schedule from the NHL API for a date range.

    Args:
        start_date: Start date string in YYYY-MM-DD format. Defaults to today.
        end_date: End date string in YYYY-MM-DD format. Defaults to start_date.

    Returns:
        List of game dictionaries with keys: game_id, home_team, away_team, start_time
    """
    if start_date is None:
        start_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if end_date is None:
        end_date = start_date

    try:
        # The NHL API uses the start date in the URL
        url = f"https://api-web.nhle.com/v1/schedule/{start_date}"
        logger.info(f"Fetching NHL schedule from {url} (start={start_date}, end={end_date})")

        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        games = []
        for game_week in data.get("gameWeek", []):
            game_date = game_week.get("date")

            # Filter by date range
            if game_date < start_date or game_date > end_date:
                continue

            for game in game_week.get("games", []):
                game_id = f"nhl-{game['id']}"
                # Get full team name (placeName + commonName)
                home_place = game["homeTeam"]["placeName"]["default"]
                home_common = game["homeTeam"].get("commonName", {}).get("default", "")
                home_team = f"{home_place} {home_common}".strip() if home_common else home_place

                away_place = game["awayTeam"]["placeName"]["default"]
                away_common = game["awayTeam"].get("commonName", {}).get("default", "")
                away_team = f"{away_place} {away_common}".strip() if away_common else away_place

                home_abbrev = game["homeTeam"]["abbrev"]
                away_abbrev = game["awayTeam"]["abbrev"]
                start_time = game["startTimeUTC"]  # ISO format timestamp

                games.append({
                    "game_id": game_id,
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_abbrev": home_abbrev,
                    "away_abbrev": away_abbrev,
                    "start_time": start_time
                })

        logger.info(f"Fetched {len(games)} NHL games for {start_date} to {end_date}")
        return games

    except Exception as e:
        logger.warning(f"Failed to fetch NHL schedule: {e}")
        return []


def init_sample_rink():
    """Initialize the sample rink (TSC Curling Club)."""
    logger.info("Initializing sample rink...")
    db = get_db()
    current_time = int(time.time())

    # Add sample rink
    db.execute("""
        INSERT OR IGNORE INTO rinks (rink_id, name, created_at)
        VALUES ('rink-tsc', 'TSC Curling Club', ?)
    """, (current_time,))

    db.commit()
    db.close()
    logger.info("Sample rink initialized")


@app.post("/admin/load-nhl-schedule")
async def load_nhl_schedule(
    start_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD, defaults to today"),
    end_date: Optional[str] = Query(None, description="End date YYYY-MM-DD, defaults to start_date")
):
    """
    Load NHL schedule from the NHL API for a date range.

    All games will be added to rink-tsc.
    """
    if start_date is None:
        start_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if end_date is None:
        end_date = start_date

    logger.info(f"Loading NHL schedule from {start_date} to {end_date}")

    # Fetch NHL games
    nhl_games = fetch_nhl_schedule(start_date, end_date)

    if not nhl_games:
        return {
            "status": "error",
            "message": f"No NHL games found for {start_date} to {end_date}"
        }

    # Add games to database
    db = get_db()
    current_time = int(time.time())

    for g in nhl_games:
        # Store game with team abbreviations
        db.execute("""
            INSERT OR REPLACE INTO games (
                game_id, rink_id, home_team, away_team, home_abbrev, away_abbrev,
                start_time, period_length_min, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            g["game_id"],
            "rink-tsc",
            g["home_team"],
            g["away_team"],
            g.get("home_abbrev"),
            g.get("away_abbrev"),
            g["start_time"],
            20,
            current_time
        ))

    # Update schedule version
    version = datetime.now(timezone.utc).isoformat()
    db.execute("""
        INSERT OR REPLACE INTO schedule_versions (rink_id, version, updated_at)
        VALUES ('rink-tsc', ?, ?)
    """, (version, current_time))

    db.commit()
    db.close()

    logger.info(f"Loaded {len(nhl_games)} NHL games")

    return {
        "status": "ok",
        "message": f"Loaded {len(nhl_games)} NHL games",
        "games_count": len(nhl_games),
        "start_date": start_date,
        "end_date": end_date
    }


def main():
    """Run the cloud API server."""
    # Configure logging first
    from score.log import init_logging
    init_logging("cloud", color="dim magenta")

    logger.info("Starting Cloud API Simulator")

    # Initialize sample rink (doesn't load games)
    init_sample_rink()

    # Run on a different port than the main app (8001 instead of 8000)
    logger.info(f"Starting cloud API server on http://{CloudConfig.HOST}:{CloudConfig.PORT}")
    uvicorn.run(app, host=CloudConfig.HOST, port=CloudConfig.PORT, log_config=None)


if __name__ == "__main__":
    main()
