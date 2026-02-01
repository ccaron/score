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
    date: Optional[str] = Query(None, description="Date in YYYY-MM-DD format (not used, kept for compatibility)")
):
    """
    Download game schedule for a specific rink.

    Returns schedule_version and list of all games for the rink.
    """
    logger.info(f"Schedule request for rink_id={rink_id}")

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

    # Query all games for the rink
    games = db.execute("""
        SELECT game_id, home_team, away_team, start_time, period_length_min
        FROM games
        WHERE rink_id = ?
        ORDER BY start_time
    """, (rink_id,)).fetchall()

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

    logger.info(f"Returning {len(games_list)} games for {rink_id}")

    return ScheduleResponse(
        schedule_version=schedule_version,
        games=games_list
    )


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
        <title>Device Management</title>
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
                padding: 20px;
                background: #f8f9fa;
                border-radius: 8px;
            }}
            .rink-section h3 {{
                font-size: 1.1em;
                margin-bottom: 15px;
                color: #495057;
            }}
            .rink-list {{
                display: flex;
                gap: 15px;
                flex-wrap: wrap;
                margin-bottom: 15px;
            }}
            .rink-item {{
                padding: 8px 16px;
                background: white;
                border: 1px solid #dee2e6;
                border-radius: 6px;
                font-size: 0.9em;
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
            <a href="/admin/heartbeats/latest">Heartbeats</a>
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
                    <div class="rink-list">
                        {''.join([f'<div class="rink-item"><strong>{r["rink_id"]}</strong>: {r["name"]}</div>' for r in rinks_list]) if rinks_list else '<div class="rink-item" style="color: #6c757d;">No rinks yet</div>'}
                    </div>
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
            <a href="/admin/heartbeats/latest">Heartbeats</a>
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
                            <th style="width: 15%;">Game ID</th>
                            <th style="width: 45%;">Teams</th>
                            <th style="width: 15%;">Clock</th>
                            <th style="width: 10%;">Status</th>
                            <th style="width: 15%;">Period Length</th>
                        </tr>
                        <tr class="filter-row">
                            <td><input type="text" id="filterGameId" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterTeams" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterClock" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterStatus" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterPeriod" placeholder="Filter..." onkeyup="filterTable()"></td>
                        </tr>
                    </thead>
                    <tbody id="gamesBody">
                        <tr>
                            <td colspan="5" class="no-games">Loading...</td>
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
                teams: document.getElementById('filterTeams').value.toLowerCase(),
                clock: document.getElementById('filterClock').value.toLowerCase(),
                status: document.getElementById('filterStatus').value.toLowerCase(),
                period: document.getElementById('filterPeriod').value.toLowerCase()
            };

            const tbody = document.getElementById('gamesBody');
            const rows = tbody.getElementsByTagName('tr');

            for (let i = 0; i < rows.length; i++) {
                const cells = rows[i].getElementsByTagName('td');
                if (cells.length < 5) continue; // Skip "no games" row

                const gameId = cells[0].textContent.toLowerCase();
                const teams = cells[1].textContent.toLowerCase();
                const clock = cells[2].textContent.toLowerCase();
                const status = cells[3].textContent.toLowerCase();
                const period = cells[4].textContent.toLowerCase();

                const match =
                    gameId.includes(filters.gameId) &&
                    teams.includes(filters.teams) &&
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
                        tbody.innerHTML = '<tr><td colspan="5" class="no-games">No games found</td></tr>';
                        return;
                    }

                    let html = '';
                    data.games.forEach(game => {
                        const status = game.clock_running ? 'running' : 'paused';
                        const statusText = game.clock_running ? 'Running' : 'Paused';
                        const clock = formatClock(game.clock_seconds);

                        html += `
                            <tr>
                                <td class="game-id">${game.game_id}</td>
                                <td>${game.home_team} vs ${game.away_team}</td>
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
                home_team = game["homeTeam"]["placeName"]["default"]
                away_team = game["awayTeam"]["placeName"]["default"]
                start_time = game["startTimeUTC"]  # ISO format timestamp

                games.append({
                    "game_id": game_id,
                    "home_team": home_team,
                    "away_team": away_team,
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
        db.execute("""
            INSERT OR REPLACE INTO games (
                game_id, rink_id, home_team, away_team, start_time,
                period_length_min, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (g["game_id"], "rink-tsc", g["home_team"], g["away_team"], g["start_time"], 20, current_time))

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
