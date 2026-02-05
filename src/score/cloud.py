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

from pathlib import Path
from fastapi import FastAPI, HTTPException, Path as FastAPIPath, Query, WebSocket
from fastapi.staticfiles import StaticFiles
import uvicorn

from score.models import (
    Game,
    ScheduleResponse,
    PostEventsRequest,
    PostEventsResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    DeviceConfigResponse,
    DeviceInfo,
    CreateDeviceRequest,
    CreateRinkRequest,
    AssignDeviceRequest,
    UpdateDeviceRequest,
    DeviceListResponse,
)

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


# ---------- Admin Navigation Helper ----------
ADMIN_NAV_ITEMS = [
    ("devices", "/admin/devices", "Devices"),
    ("games", "/admin/games/state", "Games"),
    ("events", "/admin/events", "Events"),
    ("stats", "/admin/stats", "Stats"),
    ("leagues", "/admin/leagues", "Leagues"),
    ("seasons", "/admin/seasons", "Seasons"),
    ("divisions", "/admin/divisions", "Divisions"),
    ("teams", "/admin/teams", "Teams"),
    ("players", "/admin/players", "Players"),
    ("rosters", "/admin/rosters", "Rosters"),
    ("rinks", "/admin/rinks-admin", "Rinks"),
    ("rules", "/admin/rule-sets-admin", "Rules"),
    ("officials", "/admin/officials-admin", "Officials"),
    ("tournaments", "/admin/tournaments-admin", "Tournaments"),
    ("registrations", "/admin/registrations-admin", "Registrations"),
    ("seed", "/admin/seed", "Seed"),
]


def admin_nav(active_page: str) -> str:
    """Generate admin navigation HTML with the active page highlighted."""
    links = []
    for page_id, href, label in ADMIN_NAV_ITEMS:
        css_class = ' class="active"' if page_id == active_page else ""
        links.append(f'<a href="{href}"{css_class}>{label}</a>')
    return '<div class="nav">\n            ' + '\n            '.join(links) + '\n        </div>'


def init_db():
    """Initialize cloud database schema."""
    from score.schema import init_schema
    # Set fresh_start=True to drop old tables and use new schema
    # After initial migration, set to False to preserve data
    init_schema(CLOUD_DB_PATH, fresh_start=False)


init_db()


# ---------- WebSocket state tracking ----------
websocket_clients = []


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

# Mount static files for admin CSS
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- API Endpoints ----------

@app.get("/")
async def root():
    """Root endpoint with navigation to admin pages."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/devices")


@app.get("/v1/rinks/{rink_id}/schedule", response_model=ScheduleResponse)
async def get_schedule(
    rink_id: str = FastAPIPath(..., description="Rink ID"),
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
    # Join with registrations to get organizational context
    games = db.execute("""
        SELECT DISTINCT g.game_id, g.home_team, g.away_team, g.home_abbrev, g.away_abbrev,
               g.start_time, g.period_length_min,
               l.name as league_name,
               s.name as season_name,
               d.name as division_name
        FROM games g
        LEFT JOIN team_registrations tr ON g.home_registration_id = tr.registration_id
        LEFT JOIN leagues l ON tr.league_id = l.league_id
        LEFT JOIN seasons s ON tr.season_id = s.season_id
        LEFT JOIN divisions d ON tr.division_id = d.division_id
        WHERE g.rink_id = ? AND (g.start_time LIKE ? OR g.start_time LIKE ?)
        ORDER BY g.start_time
    """, (rink_id, f"{date}%", f"{next_date}%")).fetchall()

    db.close()

    games_list = [
        Game(
            game_id=g["game_id"],
            home_team=g["home_team"],
            away_team=g["away_team"],
            home_abbrev=g["home_abbrev"],
            away_abbrev=g["away_abbrev"],
            start_time=g["start_time"],
            period_length_min=g["period_length_min"],
            league_name=g["league_name"],
            season_name=g["season_name"],
            division_name=g["division_name"],
        )
        for g in games
    ]

    logger.info(f"Returning {len(games_list)} games for {rink_id} on {date}")

    return ScheduleResponse(
        schedule_version=schedule_version,
        games=games_list
    )


@app.get("/v1/games/{game_id}/roster")
async def get_game_roster(game_id: str = FastAPIPath(..., description="Game ID")):
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
async def get_device_config(device_id: str = FastAPIPath(..., description="Device ID")):
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
        <title>score-cloud | Devices</title>
        <link rel="stylesheet" href="/static/admin.css">
    </head>
    <body>
        {admin_nav("devices")}
        <div class="container">
            <h1>Devices</h1>
            <div class="content">
                <div id="message" class="message"></div>

                <div class="hint">
                    Devices automatically register when they connect. Assign them to rinks and sheets below.
                </div>

                <table>
                    <thead>
                        <tr>
                            <th>Device ID</th>
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


@app.get("/admin/events")
async def list_events_admin(format: Optional[str] = Query(None, description="Response format: 'json' or 'html'")):
    """
    Admin page to view all received events with column filters.

    Returns HTML for browser viewing or JSON if format=json parameter is provided.
    """
    from fastapi.responses import HTMLResponse

    db = get_db()

    events = db.execute("""
        SELECT * FROM received_events
        ORDER BY received_at DESC, seq DESC
        LIMIT 1000
    """).fetchall()

    db.close()

    events_list = [dict(e) for e in events]

    # Return JSON if requested
    if format == "json":
        return {"event_count": len(events_list), "events": events_list}

    # Generate HTML view
    import datetime

    def format_timestamp(ts):
        if ts:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        return "-"

    def truncate_payload(payload, max_len=50):
        if not payload:
            return "-"
        if len(payload) > max_len:
            return payload[:max_len] + "..."
        return payload

    rows_html = ""
    if not events_list:
        rows_html = '<tr><td colspan="9" style="text-align: center; color: #666; padding: 40px;">No events found.</td></tr>'
    else:
        for e in events_list:
            rows_html += f'''
            <tr>
                <td class="event-id">{e["id"]}</td>
                <td class="game-id">{e["game_id"]}</td>
                <td class="device-id">{e["device_id"]}</td>
                <td class="session-id">{e["session_id"][:8] if e["session_id"] else "-"}...</td>
                <td>{e["seq"]}</td>
                <td><span class="event-type">{e["type"]}</span></td>
                <td class="timestamp">{e["ts_local"]}</td>
                <td class="payload" title="{e["payload"] or ""}">{truncate_payload(e["payload"])}</td>
                <td class="timestamp">{format_timestamp(e["received_at"])}</td>
            </tr>'''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Events</title>
        <link rel="stylesheet" href="/static/admin.css">
        <style>
            .event-id {{ font-family: monospace; font-size: 0.9em; }}
            .game-id {{ font-family: monospace; font-size: 0.85em; max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
            .device-id {{ font-family: monospace; font-size: 0.85em; }}
            .session-id {{ font-family: monospace; font-size: 0.85em; color: #666; }}
            .event-type {{
                display: inline-block;
                padding: 2px 8px;
                background: #e8f4fc;
                border-radius: 4px;
                font-size: 0.85em;
                font-weight: 500;
                color: #1565c0;
            }}
            .payload {{
                font-family: monospace;
                font-size: 0.8em;
                max-width: 200px;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
                color: #666;
            }}
            .payload:hover {{
                cursor: help;
            }}
        </style>
    </head>
    <body>
        {admin_nav("events")}
        <div class="container wide">
            <h1>Events</h1>
            <div class="content overflow">
                <div class="hint">
                    Showing most recent 1000 events received from devices. Hover over payload to see full content.
                </div>

                <table id="eventsTable" class="wide">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Game ID</th>
                            <th>Device ID</th>
                            <th>Session</th>
                            <th>Seq</th>
                            <th>Type</th>
                            <th>Local Time</th>
                            <th>Payload</th>
                            <th>Received</th>
                        </tr>
                        <tr class="filter-row">
                            <td><input type="text" id="filterId" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterGameId" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterDeviceId" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterSession" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterSeq" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterType" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterLocalTime" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterPayload" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterReceived" placeholder="Filter..." onkeyup="filterTable()"></td>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html}
                    </tbody>
                </table>
            </div>
        </div>

        <script>
        function filterTable() {{
            const filters = {{
                id: document.getElementById('filterId').value.toLowerCase(),
                gameId: document.getElementById('filterGameId').value.toLowerCase(),
                deviceId: document.getElementById('filterDeviceId').value.toLowerCase(),
                session: document.getElementById('filterSession').value.toLowerCase(),
                seq: document.getElementById('filterSeq').value.toLowerCase(),
                type: document.getElementById('filterType').value.toLowerCase(),
                localTime: document.getElementById('filterLocalTime').value.toLowerCase(),
                payload: document.getElementById('filterPayload').value.toLowerCase(),
                received: document.getElementById('filterReceived').value.toLowerCase()
            }};

            const rows = document.querySelectorAll('#eventsTable tbody tr');

            rows.forEach(row => {{
                if (row.cells.length < 9) return; // Skip empty row

                const id = row.cells[0].textContent.toLowerCase();
                const gameId = row.cells[1].textContent.toLowerCase();
                const deviceId = row.cells[2].textContent.toLowerCase();
                const session = row.cells[3].textContent.toLowerCase();
                const seq = row.cells[4].textContent.toLowerCase();
                const type = row.cells[5].textContent.toLowerCase();
                const localTime = row.cells[6].textContent.toLowerCase();
                const payload = row.cells[7].getAttribute('title')?.toLowerCase() || row.cells[7].textContent.toLowerCase();
                const received = row.cells[8].textContent.toLowerCase();

                const match =
                    id.includes(filters.id) &&
                    gameId.includes(filters.gameId) &&
                    deviceId.includes(filters.deviceId) &&
                    session.includes(filters.session) &&
                    seq.includes(filters.seq) &&
                    type.includes(filters.type) &&
                    localTime.includes(filters.localTime) &&
                    payload.includes(filters.payload) &&
                    received.includes(filters.received);

                row.style.display = match ? '' : 'none';
            }});
        }}
        </script>
    </body>
    </html>
    '''

    return HTMLResponse(content=html)


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
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Games</title>
        <link rel="stylesheet" href="/static/admin.css">
    </head>
    <body>
        {admin_nav("games")}
        <div class="container">
            <h1>Games</h1>
            <div class="content">
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
        function formatClock(seconds) {{
            const mins = Math.floor(seconds / 60);
            const secs = seconds % 60;
            return `${{mins}}:${{secs.toString().padStart(2, '0')}}`;
        }}

        function filterTable() {{
            const filters = {{
                gameId: document.getElementById('filterGameId').value.toLowerCase(),
                gameDate: document.getElementById('filterGameDate').value.toLowerCase(),
                teams: document.getElementById('filterTeams').value.toLowerCase(),
                score: document.getElementById('filterScore').value.toLowerCase(),
                clock: document.getElementById('filterClock').value.toLowerCase(),
                status: document.getElementById('filterStatus').value.toLowerCase(),
                period: document.getElementById('filterPeriod').value.toLowerCase()
            }};

            const tbody = document.getElementById('gamesBody');
            const rows = tbody.getElementsByTagName('tr');

            for (let i = 0; i < rows.length; i++) {{
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
            }}
        }}

        function updateGameStates() {{
            fetch('/admin/games/state?format=json')
                .then(response => response.json())
                .then(data => {{
                    const tbody = document.getElementById('gamesBody');

                    if (data.games.length === 0) {{
                        tbody.innerHTML = '<tr><td colspan="7" class="no-games">No games found</td></tr>';
                        return;
                    }}

                    let html = '';
                    data.games.forEach(game => {{
                        const status = game.clock_running ? 'running' : 'paused';
                        const statusText = game.clock_running ? 'Running' : 'Paused';
                        const clock = formatClock(game.clock_seconds);
                        const score = `${{game.home_score}} - ${{game.away_score}}`;
                        // Convert UTC timestamp to local date (handles timezone offset)
                        const startTime = new Date(game.start_time);
                        const gameDate = startTime.toLocaleDateString('en-CA'); // YYYY-MM-DD format

                        html += `
                            <tr>
                                <td class="game-id">${{game.game_id}}</td>
                                <td>${{gameDate}}</td>
                                <td>${{game.home_team}} vs ${{game.away_team}}</td>
                                <td><strong>${{score}}</strong></td>
                                <td class="clock">${{clock}}</td>
                                <td><span class="status ${{status}}">${{statusText}}</span></td>
                                <td>${{game.period_length_min}} min</td>
                            </tr>
                        `;
                    }});

                    tbody.innerHTML = html;
                }})
                .catch(error => {{
                    console.error('Error fetching game states:', error);
                }});
        }}

        // Load game states on page load
        updateGameStates();
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
    rosters = db.execute("""
        SELECT DISTINCT
            tr.team_abbrev,
            p.player_id,
            p.full_name,
            tr.roster_status,
            tr.added_at,
            tr.removed_at
        FROM team_rosters tr
        JOIN players p ON tr.player_id = p.player_id
        ORDER BY tr.team_abbrev, p.full_name
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
        rosters_html = '<tr><td colspan="5" style="text-align: center; color: #999; padding: 40px;">No rosters found.</td></tr>'
    else:
        for r in rosters:
            status_class = "active" if r["roster_status"] == "active" else "inactive"
            removed_display = "Active" if r["removed_at"] is None else format_timestamp(r["removed_at"])

            rosters_html += f'''
            <tr>
                <td class="team-abbrev">{r["team_abbrev"]}</td>
                <td>{r["full_name"]}</td>
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
        <link rel="stylesheet" href="/static/admin.css">
        <script>
            function filterTable() {{
                const filters = {{
                    team: document.getElementById('filterTeam').value.toLowerCase(),
                    name: document.getElementById('filterName').value.toLowerCase(),
                    status: document.getElementById('filterStatus').value.toLowerCase()
                }};

                const rows = document.querySelectorAll('#rostersTable tbody tr');

                rows.forEach(row => {{
                    if (row.cells.length < 3) return; // Skip empty row

                    const team = row.cells[0].textContent.toLowerCase();
                    const name = row.cells[1].textContent.toLowerCase();
                    const status = row.cells[2].textContent.toLowerCase();

                    const match =
                        team.includes(filters.team) &&
                        name.includes(filters.name) &&
                        status.includes(filters.status);

                    row.style.display = match ? '' : 'none';
                }});
            }}
        </script>
    </head>
    <body>
        {admin_nav("rosters")}
        <div class="container">
            <h1>Team Rosters</h1>
            <div class="content">
                <table id="rostersTable">
                    <thead>
                        <tr>
                            <th>Team</th>
                            <th>Player Name</th>
                            <th>Status</th>
                            <th>Added</th>
                            <th>Removed</th>
                        </tr>
                        <tr class="filter-row">
                            <td><input type="text" id="filterTeam" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterName" placeholder="Filter..." onkeyup="filterTable()"></td>
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




@app.get("/admin/teams")
async def get_teams_admin(format: Optional[str] = Query(None, description="Response format: 'json' or 'html'")):
    """
    Admin page to view all teams.

    Returns HTML for browser viewing or JSON if format=json parameter is provided.
    """
    from fastapi.responses import HTMLResponse

    db = get_db()

    teams = db.execute("""
        SELECT team_id, name, city, abbreviation, team_type, created_at
        FROM teams
        ORDER BY name
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
        teams_html = '<tr><td colspan="6" style="text-align: center; color: #999; padding: 40px;">No teams found.</td></tr>'
    else:
        for t in teams:
            teams_html += f'''
            <tr>
                <td class="team-abbrev">{t["team_id"]}</td>
                <td>{t["name"] or "-"}</td>
                <td>{t["city"] or "-"}</td>
                <td>{t["abbreviation"] or "-"}</td>
                <td>{t["team_type"] or "-"}</td>
                <td class="timestamp">{format_timestamp(t["created_at"])}</td>
            </tr>
            '''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Teams</title>
        <link rel="stylesheet" href="/static/admin.css">
        <script>
            function filterTable() {{
                const filters = {{
                    teamId: document.getElementById('filterTeamId').value.toLowerCase(),
                    name: document.getElementById('filterName').value.toLowerCase(),
                    city: document.getElementById('filterCity').value.toLowerCase(),
                    abbrev: document.getElementById('filterAbbrev').value.toLowerCase(),
                    teamType: document.getElementById('filterTeamType').value.toLowerCase()
                }};

                const rows = document.querySelectorAll('#teamsTable tbody tr');

                rows.forEach(row => {{
                    if (row.cells.length < 5) return; // Skip empty row

                    const teamId = row.cells[0].textContent.toLowerCase();
                    const name = row.cells[1].textContent.toLowerCase();
                    const city = row.cells[2].textContent.toLowerCase();
                    const abbrev = row.cells[3].textContent.toLowerCase();
                    const teamType = row.cells[4].textContent.toLowerCase();

                    const match =
                        teamId.includes(filters.teamId) &&
                        name.includes(filters.name) &&
                        city.includes(filters.city) &&
                        abbrev.includes(filters.abbrev) &&
                        teamType.includes(filters.teamType);

                    row.style.display = match ? '' : 'none';
                }});
            }}
        </script>
    </head>
    <body>
        {admin_nav("teams")}
        <div class="container">
            <h1>Teams</h1>
            <div class="content">
                <table id="teamsTable">
                    <thead>
                        <tr>
                            <th>Team ID</th>
                            <th>Name</th>
                            <th>City</th>
                            <th>Abbrev</th>
                            <th>Type</th>
                            <th>Created</th>
                        </tr>
                        <tr class="filter-row">
                            <td><input type="text" id="filterTeamId" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterName" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterCity" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterAbbrev" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterTeamType" placeholder="Filter..." onkeyup="filterTable()"></td>
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
               shoots_catches, height_inches, weight_pounds,
               birth_date, birth_city, birth_country, created_at
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
        players_html = '<tr><td colspan="11" style="text-align: center; color: #999; padding: 40px;">No players found.</td></tr>'
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
        <link rel="stylesheet" href="/static/admin.css">
        <script>
            function filterTable() {{
                const filters = {{
                    playerId: document.getElementById('filterPlayerId').value.toLowerCase(),
                    fullName: document.getElementById('filterFullName').value.toLowerCase(),
                    firstName: document.getElementById('filterFirstName').value.toLowerCase(),
                    lastName: document.getElementById('filterLastName').value.toLowerCase(),
                    shoots: document.getElementById('filterShoots').value.toLowerCase(),
                    birthCity: document.getElementById('filterBirthCity').value.toLowerCase(),
                    birthCountry: document.getElementById('filterBirthCountry').value.toLowerCase()
                }};

                const rows = document.querySelectorAll('#playersTable tbody tr');

                rows.forEach(row => {{
                    if (row.cells.length < 11) return; // Skip empty row

                    const playerId = row.cells[0].textContent.toLowerCase();
                    const fullName = row.cells[1].textContent.toLowerCase();
                    const firstName = row.cells[2].textContent.toLowerCase();
                    const lastName = row.cells[3].textContent.toLowerCase();
                    const shoots = row.cells[4].textContent.toLowerCase();
                    const birthCity = row.cells[8].textContent.toLowerCase();
                    const birthCountry = row.cells[9].textContent.toLowerCase();

                    const match =
                        playerId.includes(filters.playerId) &&
                        fullName.includes(filters.fullName) &&
                        firstName.includes(filters.firstName) &&
                        lastName.includes(filters.lastName) &&
                        shoots.includes(filters.shoots) &&
                        birthCity.includes(filters.birthCity) &&
                        birthCountry.includes(filters.birthCountry);

                    row.style.display = match ? '' : 'none';
                }});
            }}
        </script>
    </head>
    <body>
        {admin_nav("players")}
        <div class="container wide">
            <h1>Players</h1>
            <div class="content overflow">
                <div class="hint">
                    Players are created when you add them to team rosters.
                </div>

                <table id="playersTable" class="wide">
                    <thead>
                        <tr>
                            <th>Player ID</th>
                            <th>Full Name</th>
                            <th>First Name</th>
                            <th>Last Name</th>
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


# ---------- New Data Model Admin Endpoints ----------

from score.models import (
    League, Season, Division, Tournament,
    Team, Player, Rink, RinkSheet, Official,
    RuleSet, Infraction,
    TeamRegistration, RosterEntry,
)


@app.get("/admin/leagues")
async def list_leagues(format: Optional[str] = Query(None)):
    """List all leagues with HTML admin UI."""
    from fastapi.responses import HTMLResponse

    db = get_db()
    rows = db.execute("SELECT * FROM leagues ORDER BY name").fetchall()
    db.close()

    leagues = [dict(r) for r in rows]

    if format == "json":
        return {"leagues": leagues}

    # Generate HTML
    import datetime
    def format_timestamp(ts):
        if ts:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        return "-"

    rows_html = ""
    if not leagues:
        rows_html = '<tr><td colspan="6" style="text-align: center; color: #666; padding: 40px;">No leagues found.</td></tr>'
    else:
        for r in leagues:
            rows_html += f'''
            <tr>
                <td class="device-id">{r["league_id"]}</td>
                <td>{r["name"]}</td>
                <td>{r["league_type"] or "-"}</td>
                <td>{r["description"] or "-"}</td>
                <td>{r["website"] or "-"}</td>
                <td class="timestamp">{format_timestamp(r.get("created_at"))}</td>
            </tr>'''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Leagues</title>
        <link rel="stylesheet" href="/static/admin.css">
    </head>
    <body>
        {admin_nav("leagues")}
        <div class="container">
            <h1>Leagues</h1>
            <div class="content">
                <table>
                    <thead>
                        <tr>
                            <th>League ID</th>
                            <th>Name</th>
                            <th>Type</th>
                            <th>Description</th>
                            <th>Website</th>
                            <th>Created</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    '''
    return HTMLResponse(content=html)


@app.post("/admin/leagues")
async def create_league(league: League):
    """Create a new league."""
    db = get_db()
    current_time = int(time.time())
    try:
        db.execute("""
            INSERT INTO leagues (league_id, name, league_type, description, website, logo_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (league.league_id, league.name, league.league_type, league.description,
              league.website, league.logo_url, current_time))
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        raise HTTPException(status_code=409, detail=f"League {league.league_id} already exists")
    db.close()
    return {"status": "ok", "message": f"League {league.league_id} created"}


@app.get("/admin/seasons")
async def list_seasons(format: Optional[str] = Query(None)):
    """List all seasons with HTML admin UI."""
    from fastapi.responses import HTMLResponse

    db = get_db()
    rows = db.execute("SELECT * FROM seasons ORDER BY start_date DESC").fetchall()
    db.close()

    seasons = [dict(r) for r in rows]

    if format == "json":
        return {"seasons": seasons}

    # Generate HTML
    rows_html = ""
    if not seasons:
        rows_html = '<tr><td colspan="4" style="text-align: center; color: #666; padding: 40px;">No seasons found.</td></tr>'
    else:
        for r in seasons:
            rows_html += f'''
            <tr>
                <td class="device-id">{r["season_id"]}</td>
                <td>{r["name"]}</td>
                <td>{r["start_date"] or "-"}</td>
                <td>{r["end_date"] or "-"}</td>
            </tr>'''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Seasons</title>
        <link rel="stylesheet" href="/static/admin.css">
    </head>
    <body>
        {admin_nav("seasons")}
        <div class="container">
            <h1>Seasons</h1>
            <div class="content">
                <table>
                    <thead>
                        <tr>
                            <th>Season ID</th>
                            <th>Name</th>
                            <th>Start Date</th>
                            <th>End Date</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    '''
    return HTMLResponse(content=html)


@app.post("/admin/seasons")
async def create_season(season: Season):
    """Create a new season."""
    db = get_db()
    current_time = int(time.time())
    try:
        db.execute("""
            INSERT INTO seasons (season_id, name, start_date, end_date, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (season.season_id, season.name, season.start_date, season.end_date, current_time))
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        raise HTTPException(status_code=409, detail=f"Season {season.season_id} already exists")
    db.close()
    return {"status": "ok", "message": f"Season {season.season_id} created"}


@app.get("/admin/divisions")
async def list_divisions(format: Optional[str] = Query(None)):
    """List all divisions with HTML admin UI."""
    from fastapi.responses import HTMLResponse

    db = get_db()
    rows = db.execute("SELECT * FROM divisions ORDER BY name").fetchall()
    db.close()

    divisions = [dict(r) for r in rows]

    if format == "json":
        return {"divisions": divisions}

    # Generate HTML
    rows_html = ""
    if not divisions:
        rows_html = '<tr><td colspan="5" style="text-align: center; color: #666; padding: 40px;">No divisions found.</td></tr>'
    else:
        for r in divisions:
            rows_html += f'''
            <tr>
                <td class="device-id">{r["division_id"]}</td>
                <td>{r["name"]}</td>
                <td>{r["division_type"] or "-"}</td>
                <td>{r["parent_division_id"] or "-"}</td>
                <td>{r["description"] or "-"}</td>
            </tr>'''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Divisions</title>
        <link rel="stylesheet" href="/static/admin.css">
    </head>
    <body>
        {admin_nav("divisions")}
        <div class="container">
            <h1>Divisions</h1>
            <div class="content">
                <table>
                    <thead>
                        <tr>
                            <th>Division ID</th>
                            <th>Name</th>
                            <th>Type</th>
                            <th>Parent</th>
                            <th>Description</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    '''
    return HTMLResponse(content=html)


@app.post("/admin/divisions")
async def create_division(division: Division):
    """Create a new division."""
    db = get_db()
    current_time = int(time.time())
    try:
        db.execute("""
            INSERT INTO divisions (division_id, name, division_type, parent_division_id, description, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (division.division_id, division.name, division.division_type,
              division.parent_division_id, division.description, current_time))
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        raise HTTPException(status_code=409, detail=f"Division {division.division_id} already exists")
    db.close()
    return {"status": "ok", "message": f"Division {division.division_id} created"}


@app.get("/admin/rule-sets", response_model=list[RuleSet])
async def list_rule_sets():
    """List all rule sets."""
    db = get_db()
    rows = db.execute("SELECT * FROM rule_sets ORDER BY name").fetchall()
    db.close()
    return [RuleSet(
        rule_set_id=r["rule_set_id"],
        name=r["name"],
        description=r["description"],
        num_periods=r["num_periods"],
        period_length_min=r["period_length_min"],
        intermission_length_min=r["intermission_length_min"],
        overtime_length_min=r["overtime_length_min"],
        overtime_type=r["overtime_type"],
        icing_rule=r["icing_rule"],
        offside_rule=r["offside_rule"],
        body_checking=bool(r["body_checking"]),
        points_win=r["points_win"],
        points_loss=r["points_loss"],
        points_tie=r["points_tie"],
        points_otl=r["points_otl"],
        max_roster_size=r["max_roster_size"],
        min_players_to_start=r["min_players_to_start"],
        max_players_dressed=r["max_players_dressed"],
    ) for r in rows]


@app.get("/admin/rule-sets/{rule_set_id}", response_model=RuleSet)
async def get_rule_set(rule_set_id: str):
    """Get a specific rule set."""
    db = get_db()
    r = db.execute("SELECT * FROM rule_sets WHERE rule_set_id = ?", (rule_set_id,)).fetchone()
    db.close()
    if not r:
        raise HTTPException(status_code=404, detail=f"Rule set {rule_set_id} not found")
    return RuleSet(
        rule_set_id=r["rule_set_id"],
        name=r["name"],
        description=r["description"],
        num_periods=r["num_periods"],
        period_length_min=r["period_length_min"],
        intermission_length_min=r["intermission_length_min"],
        overtime_length_min=r["overtime_length_min"],
        overtime_type=r["overtime_type"],
        icing_rule=r["icing_rule"],
        offside_rule=r["offside_rule"],
        body_checking=bool(r["body_checking"]),
        points_win=r["points_win"],
        points_loss=r["points_loss"],
        points_tie=r["points_tie"],
        points_otl=r["points_otl"],
        max_roster_size=r["max_roster_size"],
        min_players_to_start=r["min_players_to_start"],
        max_players_dressed=r["max_players_dressed"],
    )


@app.get("/admin/rule-sets/{rule_set_id}/infractions", response_model=list[Infraction])
async def list_infractions(rule_set_id: str):
    """List all infractions for a rule set."""
    db = get_db()
    rows = db.execute("""
        SELECT * FROM rule_set_infractions
        WHERE rule_set_id = ?
        ORDER BY display_order, code
    """, (rule_set_id,)).fetchall()
    db.close()
    return [Infraction(
        rule_set_id=r["rule_set_id"],
        code=r["code"],
        name=r["name"],
        description=r.get("description"),
        default_severity=r["default_severity"],
        default_duration_min=r["default_duration_min"],
        allows_minor=bool(r["allows_minor"]),
        allows_major=bool(r["allows_major"]),
        allows_misconduct=bool(r["allows_misconduct"]),
        allows_match=bool(r["allows_match"]),
        is_active=bool(r["is_active"]),
        display_order=r["display_order"],
    ) for r in rows]


@app.post("/admin/teams-v2")
async def create_team_v2(team: Team):
    """Create a new team (v2 data model)."""
    db = get_db()
    current_time = int(time.time())
    try:
        db.execute("""
            INSERT INTO teams (team_id, name, city, abbreviation, team_type,
                               logo_url, primary_color, secondary_color, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (team.team_id, team.name, team.city, team.abbreviation, team.team_type,
              team.logo_url, team.primary_color, team.secondary_color, current_time))
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        raise HTTPException(status_code=409, detail=f"Team {team.team_id} already exists")
    db.close()
    return {"status": "ok", "message": f"Team {team.team_id} created"}


@app.get("/admin/teams-v2", response_model=list[Team])
async def list_teams_v2():
    """List all teams (v2 data model)."""
    db = get_db()
    rows = db.execute("SELECT * FROM teams ORDER BY name").fetchall()
    db.close()
    return [Team(
        team_id=r["team_id"],
        name=r["name"],
        city=r["city"],
        abbreviation=r["abbreviation"],
        team_type=r["team_type"],
        logo_url=r["logo_url"],
        primary_color=r["primary_color"],
        secondary_color=r["secondary_color"],
    ) for r in rows]


@app.post("/admin/team-registrations")
async def create_team_registration(reg: TeamRegistration):
    """Register a team in a league+season or tournament."""
    db = get_db()
    current_time = int(time.time())

    # Validate context
    if reg.league_id and reg.season_id and not reg.tournament_id:
        # League+Season context - OK
        pass
    elif reg.tournament_id and not reg.league_id and not reg.season_id:
        # Tournament context - OK
        pass
    else:
        db.close()
        raise HTTPException(
            status_code=400,
            detail="Must specify either (league_id + season_id) or tournament_id, not both"
        )

    try:
        db.execute("""
            INSERT INTO team_registrations
                (registration_id, team_id, league_id, season_id, tournament_id, division_id, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (reg.registration_id, reg.team_id, reg.league_id, reg.season_id,
              reg.tournament_id, reg.division_id, current_time))
        db.commit()
    except sqlite3.IntegrityError as e:
        db.close()
        raise HTTPException(status_code=409, detail=str(e))
    db.close()
    return {"status": "ok", "message": f"Team {reg.team_id} registered as {reg.registration_id}"}


@app.get("/admin/team-registrations")
async def list_team_registrations(
    league_id: Optional[str] = Query(None),
    season_id: Optional[str] = Query(None),
    tournament_id: Optional[str] = Query(None),
):
    """List team registrations, optionally filtered."""
    db = get_db()
    query = "SELECT * FROM team_registrations WHERE 1=1"
    params = []

    if league_id:
        query += " AND league_id = ?"
        params.append(league_id)
    if season_id:
        query += " AND season_id = ?"
        params.append(season_id)
    if tournament_id:
        query += " AND tournament_id = ?"
        params.append(tournament_id)

    rows = db.execute(query, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.post("/admin/roster-entries")
async def add_roster_entry(entry: RosterEntry):
    """Add a player to a team's roster."""
    db = get_db()
    current_time = int(time.time())

    try:
        db.execute("""
            INSERT INTO roster_entries
                (registration_id, player_id, jersey_number, position, roster_status,
                 is_captain, is_alternate, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (entry.registration_id, entry.player_id, entry.jersey_number, entry.position,
              entry.roster_status, 1 if entry.is_captain else 0, 1 if entry.is_alternate else 0,
              current_time))
        db.commit()
    except sqlite3.IntegrityError as e:
        db.close()
        raise HTTPException(status_code=409, detail=str(e))
    db.close()
    return {"status": "ok", "message": f"Player {entry.player_id} added to roster {entry.registration_id}"}


@app.get("/admin/roster-entries/{registration_id}")
async def get_roster_entries(registration_id: str):
    """Get all roster entries for a team registration."""
    db = get_db()
    rows = db.execute("""
        SELECT re.*, p.full_name, p.first_name, p.last_name
        FROM roster_entries re
        JOIN players p ON re.player_id = p.player_id
        WHERE re.registration_id = ? AND re.removed_at IS NULL
        ORDER BY re.jersey_number
    """, (registration_id,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ---------- New HTML Admin Pages ----------

@app.get("/admin/rinks-admin")
async def list_rinks_admin(format: Optional[str] = Query(None)):
    """List all rinks with HTML admin UI (full model)."""
    from fastapi.responses import HTMLResponse

    db = get_db()
    rows = db.execute("""
        SELECT rink_id, name, address, city, province_state, postal_code, country, phone, website, created_at
        FROM rinks ORDER BY name
    """).fetchall()
    db.close()

    rinks = [dict(r) for r in rows]

    if format == "json":
        return {"rinks": rinks}

    import datetime
    def format_timestamp(ts):
        if ts:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        return "-"

    rows_html = ""
    if not rinks:
        rows_html = '<tr><td colspan="8" style="text-align: center; color: #666; padding: 40px;">No rinks found.</td></tr>'
    else:
        for r in rinks:
            location = ", ".join(filter(None, [r["city"], r["province_state"], r["country"]])) or "-"
            rows_html += f'''
            <tr>
                <td class="device-id">{r["rink_id"]}</td>
                <td>{r["name"]}</td>
                <td>{r["address"] or "-"}</td>
                <td>{location}</td>
                <td>{r["phone"] or "-"}</td>
                <td>{r["website"] or "-"}</td>
                <td class="timestamp">{format_timestamp(r.get("created_at"))}</td>
            </tr>'''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Rinks</title>
        <link rel="stylesheet" href="/static/admin.css">
    </head>
    <body>
        {admin_nav("rinks")}
        <div class="container">
            <h1>Rinks</h1>
            <div class="content">
                <table>
                    <thead>
                        <tr>
                            <th>Rink ID</th>
                            <th>Name</th>
                            <th>Address</th>
                            <th>Location</th>
                            <th>Phone</th>
                            <th>Website</th>
                            <th>Created</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    '''
    return HTMLResponse(content=html)


@app.get("/admin/rule-sets-admin")
async def list_rule_sets_admin(format: Optional[str] = Query(None)):
    """List all rule sets with HTML admin UI."""
    from fastapi.responses import HTMLResponse

    db = get_db()
    rows = db.execute("SELECT * FROM rule_sets ORDER BY name").fetchall()
    db.close()

    rule_sets = [dict(r) for r in rows]

    if format == "json":
        return {"rule_sets": rule_sets}

    rows_html = ""
    if not rule_sets:
        rows_html = '<tr><td colspan="8" style="text-align: center; color: #666; padding: 40px;">No rule sets found.</td></tr>'
    else:
        for r in rule_sets:
            checking = "Yes" if r.get("body_checking") else "No"
            rows_html += f'''
            <tr>
                <td class="device-id">{r["rule_set_id"]}</td>
                <td>{r["name"]}</td>
                <td>{r["num_periods"]} x {r["period_length_min"]}min</td>
                <td>{r["overtime_length_min"] or "-"}min {r["overtime_type"] or ""}</td>
                <td>{r["icing_rule"]}</td>
                <td>{checking}</td>
                <td>{r["points_win"]}/{r["points_loss"]}/{r["points_tie"]}/{r["points_otl"]}</td>
                <td>{r["description"] or "-"}</td>
            </tr>'''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Rule Sets</title>
        <link rel="stylesheet" href="/static/admin.css">
    </head>
    <body>
        {admin_nav("rules")}
        <div class="container">
            <h1>Rule Sets</h1>
            <div class="content">
                <table>
                    <thead>
                        <tr>
                            <th>Rule Set ID</th>
                            <th>Name</th>
                            <th>Periods</th>
                            <th>Overtime</th>
                            <th>Icing</th>
                            <th>Checking</th>
                            <th>Points (W/L/T/OTL)</th>
                            <th>Description</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    '''
    return HTMLResponse(content=html)


@app.get("/admin/officials-admin")
async def list_officials_admin(format: Optional[str] = Query(None)):
    """List all officials with HTML admin UI."""
    from fastapi.responses import HTMLResponse

    db = get_db()
    rows = db.execute("SELECT * FROM officials ORDER BY last_name, first_name").fetchall()
    db.close()

    officials = [dict(r) for r in rows]

    if format == "json":
        return {"officials": officials}

    import datetime
    def format_timestamp(ts):
        if ts:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        return "-"

    rows_html = ""
    if not officials:
        rows_html = '<tr><td colspan="5" style="text-align: center; color: #666; padding: 40px;">No officials found.</td></tr>'
    else:
        for r in officials:
            rows_html += f'''
            <tr>
                <td class="device-id">{r["official_id"]}</td>
                <td>{r["full_name"]}</td>
                <td>{r["first_name"]}</td>
                <td>{r["last_name"]}</td>
                <td>{r["certification_level"] or "-"}</td>
            </tr>'''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Officials</title>
        <link rel="stylesheet" href="/static/admin.css">
    </head>
    <body>
        {admin_nav("officials")}
        <div class="container">
            <h1>Officials</h1>
            <div class="content">
                <table>
                    <thead>
                        <tr>
                            <th>Official ID</th>
                            <th>Full Name</th>
                            <th>First Name</th>
                            <th>Last Name</th>
                            <th>Certification</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    '''
    return HTMLResponse(content=html)


@app.get("/admin/tournaments-admin")
async def list_tournaments_admin(format: Optional[str] = Query(None)):
    """List all tournaments with HTML admin UI."""
    from fastapi.responses import HTMLResponse

    db = get_db()
    rows = db.execute("SELECT * FROM tournaments ORDER BY start_date DESC").fetchall()
    db.close()

    tournaments = [dict(r) for r in rows]

    if format == "json":
        return {"tournaments": tournaments}

    rows_html = ""
    if not tournaments:
        rows_html = '<tr><td colspan="6" style="text-align: center; color: #666; padding: 40px;">No tournaments found.</td></tr>'
    else:
        for r in tournaments:
            rows_html += f'''
            <tr>
                <td class="device-id">{r["tournament_id"]}</td>
                <td>{r["name"]}</td>
                <td>{r["tournament_type"] or "-"}</td>
                <td>{r["location"] or "-"}</td>
                <td>{r["start_date"]}</td>
                <td>{r["end_date"]}</td>
            </tr>'''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Tournaments</title>
        <link rel="stylesheet" href="/static/admin.css">
    </head>
    <body>
        {admin_nav("tournaments")}
        <div class="container">
            <h1>Tournaments</h1>
            <div class="content">
                <table>
                    <thead>
                        <tr>
                            <th>Tournament ID</th>
                            <th>Name</th>
                            <th>Type</th>
                            <th>Location</th>
                            <th>Start Date</th>
                            <th>End Date</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    '''
    return HTMLResponse(content=html)


@app.get("/admin/registrations-admin")
async def list_registrations_admin(format: Optional[str] = Query(None)):
    """List all team registrations with HTML admin UI."""
    from fastapi.responses import HTMLResponse

    db = get_db()
    rows = db.execute("""
        SELECT tr.*, t.name as team_name, t.abbreviation,
               d.name as division_name
        FROM team_registrations tr
        LEFT JOIN teams t ON tr.team_id = t.team_id
        LEFT JOIN divisions d ON tr.division_id = d.division_id
        ORDER BY tr.registered_at DESC
    """).fetchall()
    db.close()

    registrations = [dict(r) for r in rows]

    if format == "json":
        return {"registrations": registrations}

    import datetime
    def format_timestamp(ts):
        if ts:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        return "-"

    rows_html = ""
    if not registrations:
        rows_html = '<tr><td colspan="7" style="text-align: center; color: #666; padding: 40px;">No registrations found.</td></tr>'
    else:
        for r in registrations:
            context = r["league_id"] or r["tournament_id"] or "-"
            if r["season_id"]:
                context += f" / {r['season_id']}"
            rows_html += f'''
            <tr>
                <td class="device-id">{r["registration_id"]}</td>
                <td>{r["team_name"] or r["team_id"]}</td>
                <td>{r["abbreviation"] or "-"}</td>
                <td>{r["division_name"] or r["division_id"]}</td>
                <td>{context}</td>
                <td class="timestamp">{format_timestamp(r.get("registered_at"))}</td>
            </tr>'''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Team Registrations</title>
        <link rel="stylesheet" href="/static/admin.css">
    </head>
    <body>
        {admin_nav("registrations")}
        <div class="container">
            <h1>Team Registrations</h1>
            <div class="content">
                <table>
                    <thead>
                        <tr>
                            <th>Registration ID</th>
                            <th>Team</th>
                            <th>Abbrev</th>
                            <th>Division</th>
                            <th>Context (League/Season or Tournament)</th>
                            <th>Registered</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    '''
    return HTMLResponse(content=html)


# ---------- Database Seeding Admin Page ----------

from pydantic import BaseModel as PydanticBaseModel


class SeedRequest(PydanticBaseModel):
    """Request to seed database."""
    categories: list[str] = []
    player_count: int = 120
    game_count: int = 8
    seed_all: bool = False


class ClearRequest(PydanticBaseModel):
    """Request to clear database."""
    confirm: bool = False


@app.get("/admin/seed")
async def seed_admin_page():
    """Admin page for database seeding."""
    from fastapi.responses import HTMLResponse

    db = get_db()

    # Get current counts
    counts = {
        "leagues": db.execute("SELECT COUNT(*) FROM leagues").fetchone()[0],
        "seasons": db.execute("SELECT COUNT(*) FROM seasons").fetchone()[0],
        "divisions": db.execute("SELECT COUNT(*) FROM divisions").fetchone()[0],
        "rinks": db.execute("SELECT COUNT(*) FROM rinks").fetchone()[0],
        "teams": db.execute("SELECT COUNT(*) FROM teams").fetchone()[0],
        "players": db.execute("SELECT COUNT(*) FROM players").fetchone()[0],
        "registrations": db.execute("SELECT COUNT(*) FROM team_registrations").fetchone()[0],
        "rosters": db.execute("SELECT COUNT(*) FROM roster_entries").fetchone()[0],
        "games": db.execute("SELECT COUNT(*) FROM games").fetchone()[0],
    }

    db.close()

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>score-cloud | Seed Database</title>
        <link rel="stylesheet" href="/static/admin.css">
        <style>
            .seed-container {{
                display: flex;
                gap: 24px;
            }}
            .seed-options {{
                flex: 1;
                background: #fafafa;
                border: 1px solid #e0e0e0;
                border-radius: 4px;
                padding: 16px;
            }}
            .seed-options h3 {{
                margin: 0 0 12px 0;
                font-size: 12px;
                text-transform: uppercase;
                color: #666;
            }}
            .seed-option {{
                display: flex;
                align-items: center;
                gap: 8px;
                margin-bottom: 8px;
            }}
            .seed-option input[type="checkbox"] {{
                flex-shrink: 0;
                width: 16px;
                height: 16px;
            }}
            .seed-option label {{
                min-width: 140px;
            }}
            .seed-option .number-input {{
                width: 60px;
                text-align: right;
                flex-shrink: 0;
            }}
            .seed-option .count {{
                color: #666;
                font-size: 12px;
                white-space: nowrap;
            }}
            .seed-actions {{
                width: 200px;
                flex-shrink: 0;
            }}
            .seed-actions button {{
                width: 100%;
                padding: 10px 16px;
                margin-bottom: 8px;
                font-size: 13px;
            }}
            .btn-seed-all {{
                background: #1a1a2e;
                color: white;
            }}
            .btn-seed-all:hover {{
                background: #2d2d44;
            }}
            .btn-seed-selected {{
                background: #1e7e34;
                color: white;
            }}
            .btn-clear {{
                background: #dc3545;
                color: white;
            }}
            #status {{
                margin-top: 16px;
                padding: 12px;
                border-radius: 4px;
                display: none;
            }}
            #status.success {{
                background: #e6f4ea;
                color: #1e7e34;
                border: 1px solid #c3e6cb;
            }}
            #status.error {{
                background: #fce4ec;
                color: #c62828;
                border: 1px solid #f5c6cb;
            }}
        </style>
    </head>
    <body>
        {admin_nav("seed")}
        <div class="container">
            <h1>Database Seeding</h1>
            <div class="content">
                <div class="hint">
                    Seed the database with sample data for development and testing.
                    Existing data will not be overwritten.
                </div>

                <div class="seed-container">
                    <div class="seed-options">
                        <h3>Seed Options</h3>

                        <div class="seed-option">
                            <input type="checkbox" id="seed-leagues" checked>
                            <label for="seed-leagues">Leagues</label>
                            <span class="count">({counts['leagues']} existing)</span>
                        </div>

                        <div class="seed-option">
                            <input type="checkbox" id="seed-seasons" checked>
                            <label for="seed-seasons">Seasons</label>
                            <span class="count">({counts['seasons']} existing)</span>
                        </div>

                        <div class="seed-option">
                            <input type="checkbox" id="seed-divisions" checked>
                            <label for="seed-divisions">Divisions</label>
                            <span class="count">({counts['divisions']} existing)</span>
                        </div>

                        <div class="seed-option">
                            <input type="checkbox" id="seed-rinks" checked>
                            <label for="seed-rinks">Rinks</label>
                            <span class="count">({counts['rinks']} existing)</span>
                        </div>

                        <div class="seed-option">
                            <input type="checkbox" id="seed-teams" checked>
                            <label for="seed-teams">Teams</label>
                            <span class="count">({counts['teams']} existing)</span>
                        </div>

                        <div class="seed-option">
                            <input type="checkbox" id="seed-players" checked>
                            <label for="seed-players">Players</label>
                            <input type="number" id="player-count" class="number-input" value="120" min="10" max="500">
                            <span class="count">({counts['players']} existing)</span>
                        </div>

                        <div class="seed-option">
                            <input type="checkbox" id="seed-registrations" checked>
                            <label for="seed-registrations">Team Registrations</label>
                            <span class="count">({counts['registrations']} existing)</span>
                        </div>

                        <div class="seed-option">
                            <input type="checkbox" id="seed-rosters" checked>
                            <label for="seed-rosters">Roster Entries</label>
                            <span class="count">({counts['rosters']} existing)</span>
                        </div>

                        <div class="seed-option">
                            <input type="checkbox" id="seed-games" checked>
                            <label for="seed-games">Games</label>
                            <input type="number" id="game-count" class="number-input" value="8" min="1" max="50">
                            <span class="count">({counts['games']} existing)</span>
                        </div>
                    </div>

                    <div class="seed-actions">
                        <h3>Actions</h3>
                        <button class="btn-seed-all" onclick="seedAll()">Seed All</button>
                        <button class="btn-seed-selected" onclick="seedSelected()">Seed Selected</button>
                        <button class="btn-clear" onclick="clearAll()">Clear All Data</button>
                    </div>
                </div>

                <div id="status"></div>
            </div>
        </div>

        <script>
        function showStatus(message, type) {{
            const status = document.getElementById('status');
            status.textContent = message;
            status.className = type;
            status.style.display = 'block';
        }}

        function getSelectedCategories() {{
            const categories = [];
            if (document.getElementById('seed-leagues').checked) categories.push('leagues');
            if (document.getElementById('seed-seasons').checked) categories.push('seasons');
            if (document.getElementById('seed-divisions').checked) categories.push('divisions');
            if (document.getElementById('seed-rinks').checked) categories.push('rinks');
            if (document.getElementById('seed-teams').checked) categories.push('teams');
            if (document.getElementById('seed-players').checked) categories.push('players');
            if (document.getElementById('seed-registrations').checked) categories.push('registrations');
            if (document.getElementById('seed-rosters').checked) categories.push('rosters');
            if (document.getElementById('seed-games').checked) categories.push('games');
            return categories;
        }}

        async function seedAll() {{
            showStatus('Seeding all data...', 'success');

            try {{
                const response = await fetch('/admin/seed', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        seed_all: true,
                        player_count: parseInt(document.getElementById('player-count').value),
                        game_count: parseInt(document.getElementById('game-count').value)
                    }})
                }});

                const result = await response.json();

                if (response.ok) {{
                    const seeded = result.seeded;
                    const summary = Object.entries(seeded)
                        .filter(([k, v]) => v > 0)
                        .map(([k, v]) => `${{k}}: ${{v}}`)
                        .join(', ');
                    showStatus(`Seeded: ${{summary}}`, 'success');
                    setTimeout(() => location.reload(), 2000);
                }} else {{
                    showStatus(`Error: ${{result.detail || 'Failed to seed'}}`, 'error');
                }}
            }} catch (error) {{
                showStatus(`Error: ${{error.message}}`, 'error');
            }}
        }}

        async function seedSelected() {{
            const categories = getSelectedCategories();
            if (categories.length === 0) {{
                showStatus('Please select at least one category', 'error');
                return;
            }}

            showStatus('Seeding selected categories...', 'success');

            try {{
                const response = await fetch('/admin/seed', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        categories: categories,
                        player_count: parseInt(document.getElementById('player-count').value),
                        game_count: parseInt(document.getElementById('game-count').value)
                    }})
                }});

                const result = await response.json();

                if (response.ok) {{
                    const seeded = result.seeded;
                    const summary = Object.entries(seeded)
                        .filter(([k, v]) => v > 0)
                        .map(([k, v]) => `${{k}}: ${{v}}`)
                        .join(', ');
                    showStatus(`Seeded: ${{summary || 'nothing new'}}`, 'success');
                    setTimeout(() => location.reload(), 2000);
                }} else {{
                    showStatus(`Error: ${{result.detail || 'Failed to seed'}}`, 'error');
                }}
            }} catch (error) {{
                showStatus(`Error: ${{error.message}}`, 'error');
            }}
        }}

        async function clearAll() {{
            if (!confirm('Are you sure you want to clear ALL data? This cannot be undone.')) {{
                return;
            }}

            showStatus('Clearing all data...', 'success');

            try {{
                const response = await fetch('/admin/seed/clear', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ confirm: true }})
                }});

                const result = await response.json();

                if (response.ok) {{
                    showStatus('All data cleared', 'success');
                    setTimeout(() => location.reload(), 1500);
                }} else {{
                    showStatus(`Error: ${{result.detail || 'Failed to clear'}}`, 'error');
                }}
            }} catch (error) {{
                showStatus(`Error: ${{error.message}}`, 'error');
            }}
        }}
        </script>
    </body>
    </html>
    '''

    return HTMLResponse(content=html)


@app.post("/admin/seed")
async def execute_seed(request: SeedRequest):
    """Execute database seeding."""
    from score.seed import (
        seed_leagues, seed_seasons, seed_divisions, seed_rinks,
        seed_teams, seed_players, seed_league_seasons,
        seed_registrations, seed_rosters, seed_games
    )

    db = get_db()
    results = {}

    try:
        if request.seed_all:
            # Seed everything in order
            results["leagues"] = seed_leagues(db)
            results["seasons"] = seed_seasons(db)
            results["divisions"] = seed_divisions(db)
            results["rinks"] = seed_rinks(db)
            results["teams"] = seed_teams(db)
            results["players"] = seed_players(db, request.player_count)
            results["league_seasons"] = seed_league_seasons(db)
            results["registrations"] = seed_registrations(db)
            results["rosters"] = seed_rosters(db)
            results["games"] = seed_games(db, request.game_count)
        else:
            # Seed only selected categories (in dependency order)
            if "leagues" in request.categories:
                results["leagues"] = seed_leagues(db)
            if "seasons" in request.categories:
                results["seasons"] = seed_seasons(db)
            if "divisions" in request.categories:
                results["divisions"] = seed_divisions(db)
            if "rinks" in request.categories:
                results["rinks"] = seed_rinks(db)
            if "teams" in request.categories:
                results["teams"] = seed_teams(db)
            if "players" in request.categories:
                results["players"] = seed_players(db, request.player_count)
            # League seasons is implicit when seeding registrations
            if "registrations" in request.categories:
                results["league_seasons"] = seed_league_seasons(db)
                results["registrations"] = seed_registrations(db)
            if "rosters" in request.categories:
                results["rosters"] = seed_rosters(db)
            if "games" in request.categories:
                results["games"] = seed_games(db, request.game_count)

        db.commit()

    finally:
        db.close()

    logger.info(f"Database seeded: {results}")

    return {
        "status": "ok",
        "seeded": results
    }


@app.post("/admin/seed/clear")
async def clear_seed_data(request: ClearRequest):
    """Clear all seeded data from database."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="Must confirm to clear data")

    from score.seed import clear_all

    db = get_db()

    try:
        counts = clear_all(db)
        db.commit()
    finally:
        db.close()

    logger.info(f"Database cleared: {counts}")

    return {
        "status": "ok",
        "cleared": counts
    }


# ---------- Admin: Stats Query Functions ----------
def get_final_games(db, league_id=None, season_id=None, division_id=None):
    """Get list of game IDs considered 'final' for stats purposes.

    Problem: score-app doesn't update games.game_status to 'final'.
    Workaround: Consider games with GAME_END events OR games older than 3 hours
    OR just return all games matching the league/season/division filter.
    """
    # Build query based on filters
    if league_id or season_id or division_id:
        # Filter by league/season/division context
        query = """
            SELECT DISTINCT g.game_id
            FROM games g
            LEFT JOIN team_registrations hr ON g.home_registration_id = hr.registration_id
            LEFT JOIN team_registrations ar ON g.away_registration_id = ar.registration_id
            WHERE 1=1
        """
        if league_id:
            query += f" AND (hr.league_id = '{league_id}' OR ar.league_id = '{league_id}')"
        if season_id:
            query += f" AND (hr.season_id = '{season_id}' OR ar.season_id = '{season_id}')"
        if division_id:
            query += f" AND (hr.division_id = '{division_id}' OR ar.division_id = '{division_id}')"

        rows = db.execute(query).fetchall()
        game_ids = [dict(r)["game_id"] for r in rows]
        return game_ids
    else:
        # No filters - try to find games with GAME_END events
        # If none exist, return empty (will show all events unfiltered)
        query = """
            SELECT DISTINCT game_id
            FROM received_events
            WHERE type IN ('GAME_END', 'GAME_FINALIZED')
        """
        rows = db.execute(query).fetchall()
        game_ids = [dict(r)["game_id"] for r in rows]
        return game_ids if game_ids else None  # None means "no filter"


def query_top_scorers(db, league_id=None, season_id=None, division_id=None, final_only=True, limit=20):
    """Query top goal scorers from events, properly filtered by team context."""

    # Build WHERE clause for team registration filters
    filter_conditions = []
    if league_id:
        filter_conditions.append(f"tr.league_id = '{league_id}'")
    if season_id:
        filter_conditions.append(f"tr.season_id = '{season_id}'")
    if division_id:
        filter_conditions.append(f"tr.division_id = '{division_id}'")

    team_filter = f"AND ({' AND '.join(filter_conditions)})" if filter_conditions else ""

    query = f"""
        SELECT
            json_extract(e.payload, '$.scorer_id') as player_id,
            p.full_name,
            SUM(json_extract(e.payload, '$.value')) as goals
        FROM received_events e
        JOIN games g ON e.game_id = g.game_id
        LEFT JOIN team_registrations tr ON (
            CASE
                WHEN e.type = 'GOAL_HOME' THEN g.home_registration_id = tr.registration_id
                WHEN e.type = 'GOAL_AWAY' THEN g.away_registration_id = tr.registration_id
            END
        )
        LEFT JOIN players p ON json_extract(e.payload, '$.scorer_id') = p.player_id
        WHERE (e.type = 'GOAL_HOME' OR e.type = 'GOAL_AWAY')
            AND json_extract(e.payload, '$.scorer_id') IS NOT NULL
            {team_filter}
        GROUP BY player_id, p.full_name
        HAVING goals > 0
        ORDER BY goals DESC
        LIMIT {limit}
    """

    rows = db.execute(query).fetchall()
    return [dict(r) for r in rows]


def query_top_assists(db, league_id=None, season_id=None, division_id=None, final_only=True, limit=20):
    """Query top assist leaders from events, properly filtered by team context."""

    # Build WHERE clause for team registration filters
    filter_conditions = []
    if league_id:
        filter_conditions.append(f"tr.league_id = '{league_id}'")
    if season_id:
        filter_conditions.append(f"tr.season_id = '{season_id}'")
    if division_id:
        filter_conditions.append(f"tr.division_id = '{division_id}'")

    team_filter = f"AND ({' AND '.join(filter_conditions)})" if filter_conditions else ""

    # Query both assist1_id and assist2_id, combining them in a UNION
    # This sums values for both primary and secondary assists (handles cancellations)
    query = f"""
        SELECT
            assists.player_id,
            p.full_name,
            SUM(assists.value) as assists
        FROM (
            -- Primary assists
            SELECT
                json_extract(e.payload, '$.assist1_id') as player_id,
                e.game_id,
                e.type,
                json_extract(e.payload, '$.value') as value
            FROM received_events e
            WHERE (e.type = 'GOAL_HOME' OR e.type = 'GOAL_AWAY')
                AND json_extract(e.payload, '$.assist1_id') IS NOT NULL

            UNION ALL

            -- Secondary assists
            SELECT
                json_extract(e.payload, '$.assist2_id') as player_id,
                e.game_id,
                e.type,
                json_extract(e.payload, '$.value') as value
            FROM received_events e
            WHERE (e.type = 'GOAL_HOME' OR e.type = 'GOAL_AWAY')
                AND json_extract(e.payload, '$.assist2_id') IS NOT NULL
        ) assists
        JOIN games g ON assists.game_id = g.game_id
        LEFT JOIN team_registrations tr ON (
            CASE
                WHEN assists.type = 'GOAL_HOME' THEN g.home_registration_id = tr.registration_id
                WHEN assists.type = 'GOAL_AWAY' THEN g.away_registration_id = tr.registration_id
            END
        )
        LEFT JOIN players p ON assists.player_id = p.player_id
        WHERE 1=1
            {team_filter}
        GROUP BY assists.player_id, p.full_name
        HAVING assists > 0
        ORDER BY assists DESC
        LIMIT {limit}
    """

    rows = db.execute(query).fetchall()
    return [dict(r) for r in rows]


def query_top_points(db, league_id=None, season_id=None, division_id=None, final_only=True, limit=20):
    """Query top point leaders (goals + assists) from events, properly filtered by team context."""

    # Build WHERE clause for team registration filters
    filter_conditions = []
    if league_id:
        filter_conditions.append(f"tr.league_id = '{league_id}'")
    if season_id:
        filter_conditions.append(f"tr.season_id = '{season_id}'")
    if division_id:
        filter_conditions.append(f"tr.division_id = '{division_id}'")

    team_filter = f"AND ({' AND '.join(filter_conditions)})" if filter_conditions else ""

    # Combine goals and assists in a single query
    query = f"""
        SELECT
            all_points.player_id,
            p.full_name,
            SUM(points) as points,
            SUM(CASE WHEN point_type = 'goal' THEN points ELSE 0 END) as goals,
            SUM(CASE WHEN point_type = 'assist' THEN points ELSE 0 END) as assists
        FROM (
            -- Goals
            SELECT
                json_extract(e.payload, '$.scorer_id') as player_id,
                e.game_id,
                e.type,
                json_extract(e.payload, '$.value') as points,
                'goal' as point_type
            FROM received_events e
            WHERE (e.type = 'GOAL_HOME' OR e.type = 'GOAL_AWAY')
                AND json_extract(e.payload, '$.scorer_id') IS NOT NULL

            UNION ALL

            -- Primary assists
            SELECT
                json_extract(e.payload, '$.assist1_id') as player_id,
                e.game_id,
                e.type,
                json_extract(e.payload, '$.value') as points,
                'assist' as point_type
            FROM received_events e
            WHERE (e.type = 'GOAL_HOME' OR e.type = 'GOAL_AWAY')
                AND json_extract(e.payload, '$.assist1_id') IS NOT NULL

            UNION ALL

            -- Secondary assists
            SELECT
                json_extract(e.payload, '$.assist2_id') as player_id,
                e.game_id,
                e.type,
                json_extract(e.payload, '$.value') as points,
                'assist' as point_type
            FROM received_events e
            WHERE (e.type = 'GOAL_HOME' OR e.type = 'GOAL_AWAY')
                AND json_extract(e.payload, '$.assist2_id') IS NOT NULL
        ) all_points
        JOIN games g ON all_points.game_id = g.game_id
        LEFT JOIN team_registrations tr ON (
            CASE
                WHEN all_points.type = 'GOAL_HOME' THEN g.home_registration_id = tr.registration_id
                WHEN all_points.type = 'GOAL_AWAY' THEN g.away_registration_id = tr.registration_id
            END
        )
        LEFT JOIN players p ON all_points.player_id = p.player_id
        WHERE 1=1
            {team_filter}
        GROUP BY all_points.player_id, p.full_name
        HAVING points > 0
        ORDER BY points DESC, goals DESC
        LIMIT {limit}
    """

    rows = db.execute(query).fetchall()
    return [dict(r) for r in rows]


# ---------- Admin: Stats Page ----------
@app.get("/admin/stats")
async def stats_page(
    league_id: Optional[str] = Query(None),
    season_id: Optional[str] = Query(None),
    division_id: Optional[str] = Query(None),
    final_only: bool = Query(True),
    format: Optional[str] = Query(None)
):
    """Statistics leaderboards page."""
    from fastapi.responses import HTMLResponse

    db = get_db()

    # Get filter options
    leagues = db.execute("SELECT league_id, name FROM leagues ORDER BY name").fetchall()
    seasons = db.execute("SELECT season_id, name FROM seasons ORDER BY start_date DESC").fetchall()
    divisions = db.execute("SELECT division_id, name FROM divisions ORDER BY name").fetchall()

    # Query player stats
    scorers = query_top_scorers(db, league_id, season_id, division_id, final_only)
    assists_leaders = query_top_assists(db, league_id, season_id, division_id, final_only)
    points_leaders = query_top_points(db, league_id, season_id, division_id, final_only)
    penalty_leaders = []  # TODO: implement
    standings = []  # TODO: implement

    db.close()

    if format == "json":
        return {
            "scorers": scorers,
            "assists": assists_leaders,
            "points": points_leaders,
            "penalties": penalty_leaders,
            "standings": standings
        }

    # Generate HTML
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>score-cloud | Stats</title>
    <link rel="stylesheet" href="/static/admin.css">
</head>
<body>
    {admin_nav("stats")}
    <div class="container wide">
        <h1>League Statistics</h1>

        <p class="hint">Player and team statistics computed from game events. Use filters to narrow by league, season, or division.</p>

        <!-- Filters -->
        <div class="filters">
            <label>League:
                <select id="leagueFilter" onchange="applyFilters()">
                    <option value="">All Leagues</option>
                    {''.join(f'<option value="{dict(l)["league_id"]}" {"selected" if dict(l)["league_id"] == league_id else ""}>{dict(l)["name"]}</option>' for l in leagues)}
                </select>
            </label>

            <label>Season:
                <select id="seasonFilter" onchange="applyFilters()">
                    <option value="">All Seasons</option>
                    {''.join(f'<option value="{dict(s)["season_id"]}" {"selected" if dict(s)["season_id"] == season_id else ""}>{dict(s)["name"]}</option>' for s in seasons)}
                </select>
            </label>

            <label>Division:
                <select id="divisionFilter" onchange="applyFilters()">
                    <option value="">All Divisions</option>
                    {''.join(f'<option value="{dict(d)["division_id"]}" {"selected" if dict(d)["division_id"] == division_id else ""}>{dict(d)["name"]}</option>' for d in divisions)}
                </select>
            </label>

            <label>
                <input type="checkbox" id="finalOnly" {"checked" if final_only else ""} onchange="applyFilters()">
                Final games only
            </label>
        </div>

        <!-- Leaderboards -->
        <div class="content">
            <h2>Player Statistics</h2>
            {'<p style="color: #666; font-size: 13px;">No statistics found</p>' if not points_leaders else f'''
            <table id="statsTable">
                <thead>
                    <tr>
                        <th>Player</th>
                        <th class="sortable" onclick="sortTable(1)" style="cursor: pointer;">Points ▼</th>
                        <th class="sortable" onclick="sortTable(2)" style="cursor: pointer;">Goals</th>
                        <th class="sortable" onclick="sortTable(3)" style="cursor: pointer;">Assists</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(f'<tr><td>{p["full_name"] or "Unknown Player"}</td><td><strong>{p["points"]}</strong></td><td>{p["goals"]}</td><td>{p["assists"]}</td></tr>' for p in points_leaders)}
                </tbody>
            </table>
            '''}
        </div>

        <!-- Team Standings -->
        <div class="content">
            <h2>Team Standings</h2>
            <p style="color: #666; font-size: 13px;">Coming soon</p>
        </div>
    </div>

    <script>
    let sortColumn = 1;  // Default sort by Points (column 1)
    let sortAscending = false;  // Default descending

    function sortTable(column) {{
        const table = document.getElementById('statsTable');
        const tbody = table.getElementsByTagName('tbody')[0];
        const rows = Array.from(tbody.getElementsByTagName('tr'));

        // Toggle direction if clicking same column
        if (sortColumn === column) {{
            sortAscending = !sortAscending;
        }} else {{
            sortColumn = column;
            sortAscending = false;  // New column defaults to descending
        }}

        // Sort rows
        rows.sort((a, b) => {{
            let aVal = a.getElementsByTagName('td')[column].textContent.trim();
            let bVal = b.getElementsByTagName('td')[column].textContent.trim();

            // Convert to numbers for numeric columns
            if (column > 0) {{
                aVal = parseInt(aVal) || 0;
                bVal = parseInt(bVal) || 0;
            }}

            if (aVal === bVal) return 0;
            if (sortAscending) {{
                return aVal > bVal ? 1 : -1;
            }} else {{
                return aVal < bVal ? 1 : -1;
            }}
        }});

        // Re-append rows in sorted order
        rows.forEach(row => tbody.appendChild(row));

        // Update header indicators
        const headers = table.getElementsByTagName('th');
        for (let i = 1; i < headers.length; i++) {{
            const arrow = i === column ? (sortAscending ? ' ▲' : ' ▼') : '';
            headers[i].textContent = headers[i].textContent.replace(/ [▲▼]/, '') + arrow;
        }}
    }}

    function applyFilters() {{
        const league = document.getElementById('leagueFilter').value;
        const season = document.getElementById('seasonFilter').value;
        const division = document.getElementById('divisionFilter').value;
        const finalOnly = document.getElementById('finalOnly').checked;

        const params = new URLSearchParams();
        if (league) params.set('league_id', league);
        if (season) params.set('season_id', season);
        if (division) params.set('division_id', division);
        params.set('final_only', finalOnly);

        window.location.href = '/admin/stats?' + params.toString();
    }}
    </script>
</body>
</html>"""

    return HTMLResponse(content=html)


def main():
    """Run the cloud API server."""
    # Configure logging first
    from score.log import init_logging
    init_logging("cloud", color="dim magenta")

    logger.info("Starting Cloud API Simulator")

    # Run on a different port than the main app (8001 instead of 8000)
    logger.info(f"Starting cloud API server on http://{CloudConfig.HOST}:{CloudConfig.PORT}")
    uvicorn.run(app, host=CloudConfig.HOST, port=CloudConfig.PORT, log_config=None)


if __name__ == "__main__":
    main()
