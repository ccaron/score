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
from fastapi import Body, FastAPI, HTTPException, Path as FastAPIPath, Query, WebSocket, Request, Response, Form, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
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
    RosterEntry,
)
from score import auth

# Set up logger
logger = logging.getLogger("score.cloud")


# ---------- Pydantic Models for Admin Endpoints ----------

class CreatePlayerRequest(BaseModel):
    """Request to create a new player."""
    first_name: str
    last_name: str
    shoots_catches: Optional[str] = None


# ---------- Database Configuration ----------
from score.config import CloudConfig

CLOUD_DB_PATH = CloudConfig.DB_PATH


def get_db():
    """Get database connection."""
    conn = sqlite3.connect(CLOUD_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- Authentication Helpers ----------

def get_session_from_request(request: Request) -> Optional[dict]:
    """Get current session from request cookie."""
    session_id = request.cookies.get("session_id")
    if not session_id:
        return None

    db = get_db()
    try:
        session = auth.get_session(db, session_id)
        db.commit()  # Commit activity update
        return session
    finally:
        db.close()


def require_auth(request: Request) -> dict:
    """
    Require authentication for a route.

    Returns session dict if authenticated, raises HTTPException otherwise.
    """
    session = get_session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session


# ---------- Admin Navigation Helper ----------
ADMIN_NAV_ITEMS = [
    ("leagues", "/admin/organization", "Leagues"),
    ("players", "/admin/players", "Players"),
    ("venues", "/admin/rinks-admin", "Venues"),
    ("stats", "/admin/stats", "Stats"),
    ("events", "/admin/events", "Events"),
]


def admin_nav(active_page: str, session: Optional[dict] = None) -> str:
    """Generate admin navigation HTML with the active page highlighted.

    For super admins, includes a client switcher dropdown.
    """
    links = []

    # Client switcher for super admins
    client_switcher_html = ""
    if session and auth.is_super_admin(session):
        # Add System link for super admins
        css_class = ' class="active"' if active_page == "system" else ""
        links.append(f'<a href="/admin/system"{css_class}>System</a>')

        # Get current client context
        active_client_id = session.get("active_client_id")

        # Client switcher dropdown
        db = get_db()
        clients = db.execute("SELECT client_id, name FROM clients WHERE is_active = 1 ORDER BY name").fetchall()
        db.close()

        # Build dropdown options
        if active_client_id:
            # Find active client name
            active_client_name = next((c["name"] for c in clients if c["client_id"] == active_client_id), "Unknown")
            dropdown_label = f"Client: {active_client_name}"
        else:
            dropdown_label = "Client: All Clients"

        client_options = [f'<option value="">All Clients</option>']
        for client in clients:
            selected = 'selected' if client["client_id"] == active_client_id else ''
            client_options.append(f'<option value="{client["client_id"]}" {selected}>{client["name"]}</option>')

        client_switcher_html = f'''
        <form method="POST" action="/admin/switch-client" style="display: inline-block; margin-left: 16px;">
            <select name="client_id" onchange="this.form.submit()" style="font-size: 12px; padding: 4px 8px; background: #2d2d44; color: white; border: 1px solid #4a4a5e; border-radius: 3px; cursor: pointer;">
                {''.join(client_options)}
            </select>
        </form>
        '''

    # Add regular navigation links
    for page_id, href, label in ADMIN_NAV_ITEMS:
        css_class = ' class="active"' if page_id == active_page else ""
        links.append(f'<a href="{href}"{css_class}>{label}</a>')

    # User indicator (right-aligned)
    user_indicator = ""
    if session:
        role_styles = {
            "super_admin": "background: #e3f2fd; color: #1565c0;",
            "admin": "background: #e8f5e9; color: #2e7d32;",
            "viewer": "background: #fff3e0; color: #e65100;"
        }
        role_style = role_styles.get(session["role"], "")
        role_display = session["role"].replace("_", " ").title()

        user_indicator = f'''
        <div style="margin-left: auto; display: flex; align-items: center; gap: 12px; padding: 0 12px; color: #a0a0a0; font-size: 12px;">
            <span>{session["email"]}</span>
            <span class="badge" style="{role_style} font-size: 10px;">{role_display}</span>
            <a href="/admin/logout" style="color: #a0a0a0; text-decoration: none; padding: 0 8px; font-size: 12px; font-weight: 500;">Logout</a>
        </div>
        '''

    return '<div class="nav" style="display: flex; align-items: center;">\n            ' + '\n            '.join(links) + client_switcher_html + user_indicator + '\n        </div>'


def slugify(name: str) -> str:
    """Convert 'Sharks Ice at San Jose' to 'sharks-ice-at-san-jose'."""
    import re
    slug = name.lower().strip()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')


def init_db():
    """Initialize cloud database schema and create default super admin."""
    from score.schema import init_schema
    # Set fresh_start=True to drop old tables and use new schema
    # After initial migration, set to False to preserve data
    init_schema(CLOUD_DB_PATH, fresh_start=False)

    # Create default super admin if no users exist
    db = get_db()
    try:
        user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if user_count == 0:
            # Create super admin with default password (change in production!)
            auth.create_user(
                db,
                email="admin@example.com",
                password="admin123",
                client_id=None,  # No client = super admin
                role="super_admin"
            )
            db.commit()
            logger.info("Created default super admin user (email: admin@example.com, password: admin123)")
            logger.warning("IMPORTANT: Change the default admin password in production!")
    finally:
        db.close()


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

# Set up Jinja2 templates
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------- API Endpoints ----------

@app.get("/")
async def root():
    """Root endpoint with navigation to admin pages."""
    return RedirectResponse(url="/admin/devices")


# ---------- Authentication Endpoints ----------

@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None):
    """Login page."""
    # If already authenticated, redirect to admin
    session = get_session_from_request(request)
    if session:
        return RedirectResponse(url="/admin/devices", status_code=302)

    return templates.TemplateResponse("admin/login.html", {
        "request": request,
        "error": error
    })


@app.post("/admin/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    """Handle login form submission."""
    db = get_db()
    try:
        # Authenticate user
        user = auth.authenticate_user(db, email, password)
        if not user:
            db.close()
            return RedirectResponse(
                url="/admin/login?error=Invalid email or password",
                status_code=302
            )

        # Create session
        session_id = auth.create_session(db, user["user_id"], user.get("client_id"))
        db.commit()

        # Set cookie and redirect
        response = RedirectResponse(url="/admin/organization", status_code=302)
        response.set_cookie(
            key="session_id",
            value=session_id,
            httponly=True,
            max_age=auth.SESSION_DURATION_SECONDS,
            samesite="lax"
        )

        logger.info(f"User logged in: {email}")
        return response

    finally:
        db.close()


@app.get("/admin/logout")
async def logout(request: Request):
    """Logout and clear session."""
    session_id = request.cookies.get("session_id")
    if session_id:
        db = get_db()
        try:
            auth.delete_session(db, session_id)
            db.commit()
        finally:
            db.close()

    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie("session_id")
    return response


@app.post("/admin/switch-client")
async def switch_client(request: Request, client_id: str = Form("")):
    """Switch active client context for super admin."""
    session = require_auth(request)

    # Only super admins can switch clients
    if not auth.is_super_admin(session):
        raise HTTPException(status_code=403, detail="Only super admins can switch clients")

    # Update session's active_client_id
    session_id = session["session_id"]
    db = get_db()
    try:
        # Empty string means "All Clients" (None)
        active_client_id = client_id if client_id else None

        auth.update_active_client(db, session_id, active_client_id)
        db.commit()

        client_name = "All Clients" if not active_client_id else active_client_id
        logger.info(f"Super admin switched to client: {client_name}")
    finally:
        db.close()

    # Redirect back to referer or organization page
    referer = request.headers.get("referer", "/admin/organization")
    return RedirectResponse(url=referer, status_code=302)


@app.get("/admin/system")
async def system_dashboard(request: Request):
    """System dashboard for super admins."""
    session = require_auth(request)

    # Only super admins can access system dashboard
    if not auth.is_super_admin(session):
        raise HTTPException(status_code=403, detail="Only super admins can access system dashboard")

    db = get_db()

    # Fetch all clients
    clients_raw = db.execute("""
        SELECT client_id, name, slug, contact_email, is_active, created_at
        FROM clients
        ORDER BY name
    """).fetchall()

    # Fetch all users
    users_raw = db.execute("""
        SELECT u.user_id, u.email, u.role, u.client_id, u.is_active, u.last_login_at, u.created_at,
               c.name as client_name
        FROM users u
        LEFT JOIN clients c ON u.client_id = c.client_id
        ORDER BY u.created_at DESC
    """).fetchall()

    # Fetch all devices
    devices_raw = db.execute("""
        SELECT d.device_id, d.client_id, d.rink_id, d.sheet_name, d.device_name,
               d.is_assigned, d.claim_code, d.first_seen_at, d.last_seen_at,
               c.name as client_name, r.name as rink_name
        FROM devices d
        LEFT JOIN clients c ON d.client_id = c.client_id
        LEFT JOIN rinks r ON d.rink_id = r.rink_id AND d.client_id = r.client_id
        ORDER BY d.last_seen_at DESC
    """).fetchall()

    db.close()

    # Prepare clients for template
    clients = []
    for client in clients_raw:
        clients.append({
            "client_id": client["client_id"],
            "name": client["name"],
            "slug": client["slug"],
            "contact_email": client["contact_email"],
            "is_active": client["is_active"],
            "created_date": datetime.fromtimestamp(client["created_at"]).strftime("%Y-%m-%d")
        })

    # Prepare users for template
    users = []
    for user in users_raw:
        role_badge_color = {
            "super_admin": "background: #e3f2fd; color: #1565c0;",
            "admin": "background: #e8f5e9; color: #2e7d32;",
            "viewer": "background: #fff3e0; color: #e65100;"
        }.get(user["role"], "")

        users.append({
            "user_id": user["user_id"],
            "email": user["email"],
            "role": user["role"],
            "client_name": user["client_name"] or "(Super Admin)",
            "is_active": user["is_active"],
            "last_login": datetime.fromtimestamp(user["last_login_at"]).strftime("%Y-%m-%d %H:%M") if user["last_login_at"] else "Never",
            "created_date": datetime.fromtimestamp(user["created_at"]).strftime("%Y-%m-%d"),
            "role_badge_color": role_badge_color
        })

    # Prepare devices for template
    devices = []
    for device in devices_raw:
        first_seen = datetime.fromtimestamp(device["first_seen_at"]).strftime("%Y-%m-%d")
        last_seen = datetime.fromtimestamp(device["last_seen_at"]).strftime("%Y-%m-%d %H:%M")
        client_name = device["client_name"] or "<span style='color: #999;'>Unclaimed</span>"

        # Status badges
        if device["is_assigned"]:
            status_badge = '<span class="status-badge active">Assigned</span>'
            assignment_info = f"{device['rink_name'] or device['rink_id']} - {device['sheet_name']}"
        elif device["client_id"]:
            status_badge = '<span class="status-badge" style="background: #fff3e0; color: #e65100;">Claimed</span>'
            assignment_info = "<span style='color: #999;'>Not assigned</span>"
        else:
            status_badge = '<span class="status-badge" style="background: #f3e5f5; color: #6a1b9a;">Unclaimed</span>'
            assignment_info = f"<span style='color: #6a1b9a; font-family: monospace; font-weight: 600;'>{device['claim_code']}</span>" if device["claim_code"] else "-"

        # Actions
        actions = ""
        if device["client_id"]:
            actions = f'<button class="btn-unassign" onclick="unclaimDevice(\'{device["device_id"]}\')" style="font-size: 11px; padding: 3px 8px;">Unclaim</button>'

        # Device display name
        device_display = device["device_name"] if device["device_name"] else device["device_id"]
        device_id_subtitle = f'<br><small style="color: #666; font-size: 11px;">{device["device_id"]}</small>' if device["device_name"] else ""

        devices.append({
            "device_display": f'{device_display}{device_id_subtitle}',
            "client_name": client_name,
            "assignment_info": assignment_info,
            "status_badge": status_badge,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "actions": actions
        })

    return templates.TemplateResponse("admin/system.html", {
        "request": request,
        "nav_html": admin_nav("system", session),
        "wide": True,
        "clients": clients,
        "users": users,
        "devices": devices
    })


@app.post("/admin/clients")
async def create_client(
    request: Request,
    client_id: str = Form(...),
    name: str = Form(...),
    slug: str = Form(...),
    contact_email: str = Form("")
):
    """Create a new client."""
    session = require_auth(request)

    # Only super admins can create clients
    if not auth.is_super_admin(session):
        raise HTTPException(status_code=403, detail="Only super admins can create clients")

    db = get_db()
    current_time = int(time.time())

    # Empty string should become None for optional fields
    contact_email_value = contact_email if contact_email else None

    try:
        db.execute("""
            INSERT INTO clients (client_id, name, slug, contact_email, is_active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
        """, (client_id, name, slug, contact_email_value, current_time))
        db.commit()
        logger.info(f"Created client: {name} ({client_id})")
    except sqlite3.IntegrityError as e:
        db.close()
        raise HTTPException(status_code=400, detail=f"Client ID or slug already exists: {e}")
    finally:
        db.close()

    return RedirectResponse(url="/admin/system", status_code=302)


@app.post("/admin/users")
async def create_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    client_id: str = Form("")
):
    """Create a new user."""
    session = require_auth(request)

    # Only super admins can create users
    if not auth.is_super_admin(session):
        raise HTTPException(status_code=403, detail="Only super admins can create users")

    # Validate role
    if role not in ["super_admin", "admin", "viewer"]:
        raise HTTPException(status_code=400, detail="Invalid role")

    # Empty string should become None for client_id
    client_id_value = client_id if client_id else None

    # If not super_admin role, must have a client_id
    if role != "super_admin" and not client_id_value:
        raise HTTPException(status_code=400, detail="Non-super admin users must be assigned to a client")

    db = get_db()

    try:
        user_id = auth.create_user(db, email, password, client_id_value, role)
        db.commit()
        logger.info(f"Created user: {email} (role: {role}, client: {client_id_value or 'super_admin'})")
    except sqlite3.IntegrityError:
        db.close()
        raise HTTPException(status_code=400, detail="Email already exists")
    finally:
        db.close()

    return RedirectResponse(url="/admin/system", status_code=302)


@app.post("/admin/reset-password")
async def reset_user_password(
    request: Request,
    user_id: str = Form(...),
    new_password: str = Form(...)
):
    """Reset a user's password (super admin only)."""
    session = require_auth(request)

    # Only super admins can reset passwords
    if not auth.is_super_admin(session):
        raise HTTPException(status_code=403, detail="Only super admins can reset passwords")

    # Validate password length
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    db = get_db()

    try:
        # Check if user exists
        user = db.execute("SELECT user_id, email FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Hash new password and update
        password_hash = auth.hash_password(new_password)
        db.execute("""
            UPDATE users SET password_hash = ? WHERE user_id = ?
        """, (password_hash, user_id))
        db.commit()

        logger.info(f"Password reset for user: {user['email']} by super admin")
    finally:
        db.close()

    return RedirectResponse(url="/admin/system", status_code=302)


@app.post("/admin/toggle-client-status")
async def toggle_client_status(
    request: Request,
    client_id: str = Form(...),
    is_active: int = Form(...)
):
    """Toggle client active status (super admin only)."""
    session = require_auth(request)

    # Only super admins can toggle client status
    if not auth.is_super_admin(session):
        raise HTTPException(status_code=403, detail="Only super admins can toggle client status")

    db = get_db()

    try:
        # Check if client exists
        client = db.execute("SELECT client_id, name, is_active FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")

        # Update status
        db.execute("""
            UPDATE clients SET is_active = ? WHERE client_id = ?
        """, (is_active, client_id))
        db.commit()

        action = "activated" if is_active else "deactivated"
        logger.info(f"Client {action}: {client['name']} ({client_id}) by super admin")
    finally:
        db.close()

    return RedirectResponse(url="/admin/system", status_code=302)


@app.post("/admin/toggle-user-status")
async def toggle_user_status(
    request: Request,
    user_id: str = Form(...),
    is_active: int = Form(...)
):
    """Toggle user active status (super admin only)."""
    session = require_auth(request)

    # Only super admins can toggle user status
    if not auth.is_super_admin(session):
        raise HTTPException(status_code=403, detail="Only super admins can toggle user status")

    db = get_db()

    try:
        # Check if user exists
        user = db.execute("SELECT user_id, email, is_active FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Prevent deactivating yourself
        if user_id == session["user_id"] and not is_active:
            raise HTTPException(status_code=400, detail="Cannot deactivate your own account")

        # Update status
        db.execute("""
            UPDATE users SET is_active = ? WHERE user_id = ?
        """, (is_active, user_id))
        db.commit()

        action = "activated" if is_active else "deactivated"
        logger.info(f"User {action}: {user['email']} by super admin")
    finally:
        db.close()

    return RedirectResponse(url="/admin/system", status_code=302)


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
async def get_device_config(
    device_id: str = FastAPIPath(..., description="Device ID"),
    claim_code: Optional[str] = Query(None, description="6-character claim code displayed on device")
):
    """
    Get configuration for a device.

    Returns device assignment (rink_id, sheet_name) if assigned,
    or registers the device as unassigned if first time seeing it.

    If claim_code is provided, stores it for client claiming.
    """
    logger.info(f"Config request from device_id={device_id}, claim_code={claim_code}")

    db = get_db()
    current_time = int(time.time())
    claim_code_expiry = current_time + (24 * 60 * 60)  # 24 hours from now

    # Check if device exists
    device = db.execute(
        "SELECT * FROM devices WHERE device_id = ?",
        (device_id,)
    ).fetchone()

    if device:
        # Update last_seen_at and claim_code if provided
        if claim_code and not device["client_id"]:  # Only update claim code if unclaimed
            db.execute("""
                UPDATE devices
                SET last_seen_at = ?, claim_code = ?, claim_code_expires_at = ?
                WHERE device_id = ?
            """, (current_time, claim_code, claim_code_expiry, device_id))
        else:
            db.execute(
                "UPDATE devices SET last_seen_at = ? WHERE device_id = ?",
                (current_time, device_id)
            )
        db.commit()

        is_claimed = device["client_id"] is not None
        is_assigned = bool(device["is_assigned"])

        if is_assigned:
            logger.info(f"Device {device_id} is assigned to rink={device['rink_id']}, sheet={device['sheet_name']}")
            db.close()
            return DeviceConfigResponse(
                device_id=device_id,
                is_claimed=is_claimed,
                is_assigned=True,
                rink_id=device["rink_id"],
                sheet_name=device["sheet_name"],
                device_name=device["device_name"],
                message=f"Assigned to {device['rink_id']} - {device['sheet_name']}"
            )
        else:
            logger.info(f"Device {device_id} exists but is not assigned (claimed={is_claimed})")
            db.close()
            if is_claimed:
                return DeviceConfigResponse(
                    device_id=device_id,
                    is_claimed=True,
                    is_assigned=False,
                    message="Device claimed. Waiting for admin to assign to rink/sheet."
                )
            else:
                return DeviceConfigResponse(
                    device_id=device_id,
                    is_claimed=False,
                    is_assigned=False,
                    message="Waiting to be claimed. Enter claim code in admin panel."
                )
    else:
        # First time seeing this device - register it as unassigned and unclaimed
        logger.info(f"New device {device_id} - registering with claim_code={claim_code}")
        db.execute("""
            INSERT INTO devices (device_id, is_assigned, claim_code, claim_code_expires_at, first_seen_at, last_seen_at)
            VALUES (?, 0, ?, ?, ?, ?)
        """, (device_id, claim_code, claim_code_expiry if claim_code else None, current_time, current_time))
        db.commit()
        db.close()

        return DeviceConfigResponse(
            device_id=device_id,
            is_claimed=False,
            is_assigned=False,
            message="Device registered. Waiting to be claimed with claim code."
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
async def create_rink(
    request: Request,
    name: str = Body(...),
    address: Optional[str] = Body(None),
    city: Optional[str] = Body(None),
    province_state: Optional[str] = Body(None),
    postal_code: Optional[str] = Body(None),
    country: Optional[str] = Body(None),
    phone: Optional[str] = Body(None),
    website: Optional[str] = Body(None)
):
    """
    Create a new venue (rink).

    Auto-generates rink_id from name using slugify.
    """
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    # Auto-generate rink_id from name
    rink_id = slugify(name)

    logger.info(f"Creating rink {rink_id} from name '{name}' for client {client_id}")

    db = get_db()
    current_time = int(time.time())

    # Check if rink already exists for this client
    if client_id:
        existing = db.execute(
            "SELECT rink_id FROM rinks WHERE client_id = ? AND rink_id = ?",
            (client_id, rink_id)
        ).fetchone()
    else:
        existing = db.execute(
            "SELECT rink_id FROM rinks WHERE rink_id = ?",
            (rink_id,)
        ).fetchone()

    if existing:
        db.close()
        raise HTTPException(
            status_code=409,
            detail=f"Rink {rink_id} already exists"
        )

    # Insert rink
    db.execute("""
        INSERT INTO rinks (client_id, rink_id, name, address, city, province_state, postal_code, country, phone, website, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (client_id, rink_id, name, address, city, province_state, postal_code, country, phone, website, current_time))

    db.commit()
    db.close()

    logger.info(f"Successfully created rink {rink_id}")

    return {
        "status": "ok",
        "message": f"Rink {rink_id} created",
        "rink": {
            "rink_id": rink_id,
            "name": name,
            "address": address,
            "city": city,
            "province_state": province_state,
            "postal_code": postal_code,
            "country": country,
            "phone": phone,
            "website": website
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


# ---------- Rink Sheets Admin Endpoints ----------

@app.post("/admin/rink-sheets")
async def create_rink_sheet(request: Request):
    """
    Create a new sheet within a rink.

    Auto-generates sheet_id from rink_id + slugified name.
    """
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    if not client_id:
        raise HTTPException(status_code=400, detail="No client selected")

    # Parse request body
    try:
        body = await request.json()
        rink_id = body.get("rink_id")
        name = body.get("name")
        surface_type = body.get("surface_type")
        capacity = body.get("capacity")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid request body: {str(e)}")

    if not rink_id or not name:
        raise HTTPException(status_code=400, detail="rink_id and name are required")

    logger.info(f"Creating sheet {name} for rink {rink_id} (client: {client_id})")

    db = get_db()

    # Verify rink exists and belongs to this client
    rink = db.execute(
        "SELECT rink_id, client_id FROM rinks WHERE rink_id = ? AND client_id = ?",
        (rink_id, client_id)
    ).fetchone()
    if not rink:
        db.close()
        raise HTTPException(status_code=404, detail=f"Rink {rink_id} not found or access denied")

    # Auto-generate sheet_id
    sheet_id = f"{rink_id}-{slugify(name)}"

    # Check if sheet already exists
    existing = db.execute(
        "SELECT sheet_id FROM rink_sheets WHERE client_id = ? AND sheet_id = ?",
        (client_id, sheet_id)
    ).fetchone()

    if existing:
        db.close()
        raise HTTPException(
            status_code=409,
            detail=f"Sheet {sheet_id} already exists"
        )

    current_time = int(time.time())

    # Insert sheet with client_id
    db.execute("""
        INSERT INTO rink_sheets (client_id, rink_id, sheet_id, name, surface_type, capacity, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (client_id, rink_id, sheet_id, name, surface_type, capacity, current_time))

    db.commit()
    db.close()

    logger.info(f"Successfully created sheet {sheet_id}")

    return {
        "status": "ok",
        "message": f"Sheet {sheet_id} created",
        "sheet": {
            "sheet_id": sheet_id,
            "rink_id": rink_id,
            "name": name,
            "surface_type": surface_type,
            "capacity": capacity
        }
    }


@app.get("/admin/rink-sheets/{rink_id}")
async def get_rink_sheets(rink_id: str):
    """Get all sheets for a specific rink."""
    db = get_db()

    sheets = db.execute("""
        SELECT sheet_id, rink_id, name, surface_type, capacity, created_at
        FROM rink_sheets
        WHERE rink_id = ?
        ORDER BY name
    """, (rink_id,)).fetchall()

    db.close()

    return {
        "sheets": [dict(s) for s in sheets]
    }


@app.delete("/admin/rink-sheets/{sheet_id}")
async def delete_rink_sheet(request: Request, sheet_id: str):
    """
    Delete a sheet.

    This will fail if there are devices assigned to this sheet.
    """
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    if not client_id:
        raise HTTPException(status_code=400, detail="No client selected")

    logger.info(f"Deleting sheet {sheet_id} (client: {client_id})")

    db = get_db()

    # Check if sheet exists and belongs to this client
    sheet = db.execute(
        "SELECT sheet_id, rink_id, name, client_id FROM rink_sheets WHERE sheet_id = ? AND client_id = ?",
        (sheet_id, client_id)
    ).fetchone()

    if not sheet:
        db.close()
        raise HTTPException(status_code=404, detail=f"Sheet {sheet_id} not found or access denied")

    # Check if any devices are assigned to this sheet
    # Devices reference sheets by rink_id + sheet_name (not sheet_id FK)
    devices = db.execute(
        "SELECT COUNT(*) as count FROM devices WHERE client_id = ? AND rink_id = ? AND sheet_name = ? AND is_assigned = 1",
        (client_id, sheet["rink_id"], sheet["name"])
    ).fetchone()

    if devices["count"] > 0:
        db.close()
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete sheet {sheet_id}: {devices['count']} device(s) are assigned to it. Unassign devices first."
        )

    # Delete the sheet
    db.execute("DELETE FROM rink_sheets WHERE sheet_id = ? AND client_id = ?", (sheet_id, client_id))

    db.commit()
    db.close()

    logger.info(f"Successfully deleted sheet {sheet_id}")

    return {
        "status": "ok",
        "message": f"Sheet {sheet_id} deleted"
    }




@app.get("/admin/devices")
async def list_devices(request: Request):
    """List all devices."""
    from fastapi.responses import HTMLResponse

    session = require_auth(request)
    client_id = auth.get_current_client(session)

    db = get_db()

    # Fetch devices filtered by client
    if client_id:
        devices = db.execute("""
            SELECT device_id, client_id, rink_id, sheet_name, device_name, is_assigned,
                   first_seen_at, last_seen_at, notes
            FROM devices
            WHERE client_id = ? OR client_id IS NULL
            ORDER BY is_assigned DESC, last_seen_at DESC
        """, (client_id,)).fetchall()
    else:
        # Super admin viewing all devices
        devices = db.execute("""
            SELECT device_id, client_id, rink_id, sheet_name, device_name, is_assigned,
                   first_seen_at, last_seen_at, notes
            FROM devices
            ORDER BY is_assigned DESC, last_seen_at DESC
        """).fetchall()

    db.close()

    # Build device rows
    device_rows = []
    for device in devices:
        status = '<span class="badge assigned">Assigned</span>' if device["is_assigned"] else '<span class="badge unassigned">Unassigned</span>'
        client_info = device["client_id"] or "-"
        rink_info = device["rink_id"] or "-"
        sheet_info = device["sheet_name"] or "-"
        last_seen = datetime.fromtimestamp(device["last_seen_at"]).strftime("%Y-%m-%d %H:%M") if device["last_seen_at"] else "Never"

        device_rows.append(f"""
            <tr>
                <td class="device-id">{device["device_id"]}</td>
                <td>{client_info}</td>
                <td>{rink_info}</td>
                <td>{sheet_info}</td>
                <td>{device["device_name"] or "-"}</td>
                <td>{status}</td>
                <td class="timestamp">{last_seen}</td>
            </tr>
        """)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>score-cloud | Devices</title>
    <link rel="stylesheet" href="/static/admin.css">
</head>
<body>
    {admin_nav("devices", session)}
    <div class="container wide">
        <h1>Devices</h1>
        <p class="hint">Claim new devices using the code displayed on the device screen. Assign devices to rinks in the Venues page.</p>

        <!-- Claim Device Form -->
        <div class="content" style="margin-bottom: 16px;">
            <h2 style="font-size: 14px; margin-bottom: 8px;">Claim New Device</h2>
            <form method="POST" action="/admin/devices/claim" style="display: flex; gap: 8px; align-items: center;">
                <div style="flex: 0 0 200px;">
                    <input type="text" name="claim_code" required placeholder="ABC-123" maxlength="7" style="text-transform: uppercase; font-family: monospace; font-size: 14px; padding: 6px 10px;">
                </div>
                <button type="submit" class="btn-save">Claim Device</button>
                <span style="color: #666; font-size: 12px;">Enter the 6-character code shown on the device</span>
            </form>
        </div>

        <div class="content">
            <table>
                <thead>
                    <tr>
                        <th>Device ID</th>
                        <th>Client</th>
                        <th>Rink</th>
                        <th>Sheet</th>
                        <th>Name</th>
                        <th>Status</th>
                        <th>Last Seen</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(device_rows) if device_rows else '<tr><td colspan="7" style="text-align: center; color: #666;">No devices found</td></tr>'}
                </tbody>
            </table>
        </div>
    </div>
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
async def update_device(request: Request, device_id: str, update_request: UpdateDeviceRequest):
    """
    Update a device's assignment and details.

    To assign an unassigned device, provide rink_id and sheet_name.
    To update an existing assignment, provide any fields you want to change.
    To unassign, use DELETE /admin/devices/{device_id}/assignment instead.
    """
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    logger.info(f"Updating device {device_id}")

    db = get_db()

    # Check if device exists
    device = db.execute(
        "SELECT device_id, client_id FROM devices WHERE device_id = ?",
        (device_id,)
    ).fetchone()

    if not device:
        db.close()
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found. Device must connect at least once before assignment.")

    # If device is already assigned, validate client ownership (unless super admin viewing all)
    if device["client_id"] and client_id:
        if device["client_id"] != client_id:
            db.close()
            raise HTTPException(status_code=403, detail="Cannot update device assigned to different client")

    # Build update query dynamically based on provided fields
    updates = []
    params = []

    if update_request.rink_id is not None:
        # Validate rink exists and get its client_id
        if client_id:
            rink = db.execute(
                "SELECT rink_id, client_id FROM rinks WHERE rink_id = ? AND client_id = ?",
                (update_request.rink_id, client_id)
            ).fetchone()
        else:
            # Super admin can assign to any rink
            rink = db.execute(
                "SELECT rink_id, client_id FROM rinks WHERE rink_id = ?",
                (update_request.rink_id,)
            ).fetchone()

        if not rink:
            db.close()
            raise HTTPException(status_code=404, detail=f"Rink {update_request.rink_id} not found in client context")

        updates.append("rink_id = ?")
        params.append(update_request.rink_id)

        # Inherit rink's client_id
        updates.append("client_id = ?")
        params.append(rink["client_id"])

    if update_request.sheet_name is not None:
        updates.append("sheet_name = ?")
        params.append(update_request.sheet_name)

    if update_request.device_name is not None:
        updates.append("device_name = ?")
        params.append(update_request.device_name)

    if update_request.notes is not None:
        updates.append("notes = ?")
        params.append(update_request.notes)

    # If rink_id and sheet_name are both provided, mark as assigned
    if update_request.rink_id is not None and update_request.sheet_name is not None:
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
async def unassign_device(request: Request, device_id: str):
    """Clear a device's assignment (unassign from rink and sheet)."""
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    logger.info(f"Unassigning device {device_id}")

    db = get_db()

    # Check if device exists and validate client ownership
    device = db.execute(
        "SELECT device_id, client_id FROM devices WHERE device_id = ?",
        (device_id,)
    ).fetchone()

    if not device:
        db.close()
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found")

    # Validate client ownership (unless super admin viewing all)
    if device["client_id"] and client_id:
        if device["client_id"] != client_id:
            db.close()
            raise HTTPException(status_code=403, detail="Cannot unassign device from different client")

    # Unassign the device (clear rink_id, sheet_name, but keep client_id)
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
async def delete_device(request: Request, device_id: str):
    """Completely delete a device from the database."""
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    logger.info(f"Deleting device {device_id}")

    db = get_db()

    # Check if device exists and validate ownership
    device = db.execute(
        "SELECT device_id, client_id FROM devices WHERE device_id = ?",
        (device_id,)
    ).fetchone()

    if not device:
        db.close()
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found")

    # Validate client ownership (unless super admin viewing all)
    if device["client_id"] and client_id:
        if device["client_id"] != client_id:
            db.close()
            raise HTTPException(status_code=403, detail="Cannot delete device from different client")

    # Delete the device (this will cascade delete deliveries if we had FK constraints)
    db.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))

    db.commit()
    db.close()

    logger.info(f"Successfully deleted device {device_id}")

    return {
        "status": "ok",
        "message": f"Device {device_id} deleted"
    }


@app.post("/admin/devices/claim")
async def claim_device(request: Request, claim_code: str = Form(...), target_client_id: str = Form(""), device_name: str = Form("")):
    """Claim an unclaimed device using its claim code."""
    # Require authentication
    session = require_auth(request)

    # Determine which client to claim for
    if target_client_id:
        # Super admin claiming for a specific client
        if not auth.is_super_admin(session):
            raise HTTPException(status_code=403, detail="Only super admins can claim devices for other clients")
        client_id = target_client_id
    else:
        # Regular admin claiming for their own client
        client_id = auth.get_current_client(session)
        if not client_id:
            raise HTTPException(status_code=400, detail="Must specify target client or have active client selected")

    claim_code = claim_code.strip().upper()  # Normalize code
    logger.info(f"Client {client_id} attempting to claim device with code {claim_code}")

    db = get_db()
    current_time = int(time.time())

    # Find device with matching claim code
    device = db.execute("""
        SELECT device_id, client_id, claim_code, claim_code_expires_at
        FROM devices
        WHERE claim_code = ?
    """, (claim_code,)).fetchone()

    if not device:
        db.close()
        raise HTTPException(status_code=404, detail="Invalid claim code. Check the code on your device screen.")

    # Check if already claimed
    if device["client_id"]:
        db.close()
        raise HTTPException(status_code=400, detail="Device already claimed by another client")

    # Check if code expired
    if device["claim_code_expires_at"] and device["claim_code_expires_at"] < current_time:
        db.close()
        raise HTTPException(status_code=400, detail="Claim code expired. Restart the device to generate a new code.")

    # Claim the device
    db.execute("""
        UPDATE devices
        SET client_id = ?, claim_code = NULL, claim_code_expires_at = NULL, device_name = ?
        WHERE device_id = ?
    """, (client_id, device_name if device_name else None, device["device_id"]))
    db.commit()
    db.close()

    logger.info(f"Device {device['device_id']} claimed by client {client_id}")

    # Redirect back to Venues page
    return RedirectResponse(url="/admin/rinks-admin", status_code=302)


@app.post("/admin/devices/{device_id}/unclaim")
async def unclaim_device(request: Request, device_id: str):
    """Unclaim a device and generate a new claim code."""
    # Require authentication (super admin only)
    session = require_auth(request)

    if not auth.is_super_admin(session):
        raise HTTPException(status_code=403, detail="Only super admins can unclaim devices")

    logger.info(f"Super admin unclaiming device {device_id}")

    db = get_db()
    current_time = int(time.time())

    # Check if device exists
    device = db.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()

    if not device:
        db.close()
        raise HTTPException(status_code=404, detail="Device not found")

    # Generate new claim code
    import random
    import string
    letters = ''.join(random.choices(string.ascii_uppercase, k=3))
    digits = ''.join(random.choices(string.digits, k=3))
    new_claim_code = f"{letters}-{digits}"
    claim_code_expiry = current_time + (24 * 60 * 60)  # 24 hours from now

    # Unclaim the device: clear client_id, assignment, generate new claim code
    db.execute("""
        UPDATE devices
        SET client_id = NULL,
            rink_id = NULL,
            sheet_name = NULL,
            device_name = NULL,
            is_assigned = 0,
            claim_code = ?,
            claim_code_expires_at = ?
        WHERE device_id = ?
    """, (new_claim_code, claim_code_expiry, device_id))
    db.commit()
    db.close()

    logger.info(f"Device {device_id} unclaimed, new claim code: {new_claim_code}")

    return RedirectResponse(url="/admin/system", status_code=302)


# Keep legacy endpoints for backwards compatibility
@app.post("/admin/devices/{device_id}/assign")
async def assign_device_legacy(req: Request, device_id: str, assign_request: AssignDeviceRequest):
    """Legacy endpoint - use PUT /admin/devices/{device_id} instead."""
    return await update_device(req, device_id, UpdateDeviceRequest(
        rink_id=assign_request.rink_id,
        sheet_name=assign_request.sheet_name,
        device_name=assign_request.device_name,
        notes=assign_request.notes
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
async def list_events_admin(request: Request, format: Optional[str] = Query(None, description="Response format: 'json' or 'html'")):
    """
    Admin page to view all received events with column filters.

    Returns HTML for browser viewing or JSON if format=json parameter is provided.
    """
    from fastapi.responses import HTMLResponse

    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    db = get_db()

    # Fetch events (filtered by client's games)
    if client_id:
        events = db.execute("""
            SELECT e.* FROM received_events e
            JOIN games g ON e.game_id = g.game_id
            WHERE g.client_id = ?
            ORDER BY e.received_at DESC, e.seq DESC
            LIMIT 1000
        """, (client_id,)).fetchall()
    else:
        # Super admin viewing all clients
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
        {admin_nav("events", session)}
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
    Legacy Schedule page - redirects to unified Leagues page.

    For JSON format, returns flattened game states for backwards compatibility.
    """
    from fastapi.responses import RedirectResponse

    # Support JSON API for backwards compatibility
    if format == "json":
        db = get_db()

        # Flatten all games with their states
        all_games = []
        games = db.execute("SELECT game_id FROM games").fetchall()
        for game in games:
            state = reconstruct_game_state(game["game_id"])
            if state:
                all_games.append(state)

        db.close()

        return {
            "game_count": len(all_games),
            "games": all_games
        }

    # HTML requests redirect to Leagues page
    return RedirectResponse(url="/admin/organization")


@app.get("/admin/organization")
async def get_organization_admin(request: Request):
    """
    Admin page showing organizational hierarchy: Leagues → Seasons → Divisions → Teams → Rosters.

    Provides tree view with expandable sections and forms to create entities at each level.
    """
    from fastapi.responses import HTMLResponse

    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    db = get_db()

    # Fetch all leagues (filtered by client)
    if client_id:
        leagues = db.execute(
            "SELECT league_id, name, league_type, description FROM leagues WHERE client_id = ? ORDER BY name",
            (client_id,)
        ).fetchall()
    else:
        # Super admin viewing all clients
        leagues = db.execute("SELECT league_id, name, league_type, description FROM leagues ORDER BY name").fetchall()

    # Build nested data structure
    org_data = []

    for league in leagues:
        league_dict = dict(league)

        # Fetch seasons for this league
        if client_id:
            seasons = db.execute("""
                SELECT s.season_id, s.name, s.start_date, s.end_date
                FROM seasons s
                JOIN league_seasons ls ON s.season_id = ls.season_id AND s.client_id = ls.client_id
                WHERE ls.league_id = ? AND ls.client_id = ?
                ORDER BY s.start_date DESC
            """, (league["league_id"], client_id)).fetchall()
        else:
            seasons = db.execute("""
                SELECT s.season_id, s.name, s.start_date, s.end_date
                FROM seasons s
                JOIN league_seasons ls ON s.season_id = ls.season_id
                WHERE ls.league_id = ?
                ORDER BY s.start_date DESC
            """, (league["league_id"],)).fetchall()

        league_dict["seasons"] = []

        for season in seasons:
            season_dict = dict(season)

            # Fetch divisions for this league+season (via league_season_divisions)
            if client_id:
                divisions = db.execute("""
                    SELECT d.division_id, d.name
                    FROM divisions d
                    JOIN league_season_divisions lsd ON d.division_id = lsd.division_id AND d.client_id = lsd.client_id
                    WHERE lsd.league_id = ? AND lsd.season_id = ? AND lsd.client_id = ?
                    ORDER BY lsd.display_order, d.name
                """, (league["league_id"], season["season_id"], client_id)).fetchall()
            else:
                divisions = db.execute("""
                    SELECT d.division_id, d.name
                    FROM divisions d
                    JOIN league_season_divisions lsd ON d.division_id = lsd.division_id
                    WHERE lsd.league_id = ? AND lsd.season_id = ?
                    ORDER BY lsd.display_order, d.name
                """, (league["league_id"], season["season_id"])).fetchall()

            season_dict["divisions"] = []

            for division in divisions:
                division_dict = dict(division)

                # Fetch team registrations for this division
                if client_id:
                    registrations = db.execute("""
                        SELECT registration_id, team_name, abbreviation,
                               (SELECT COUNT(*) FROM roster_entries re
                                WHERE re.registration_id = tr.registration_id AND re.removed_at IS NULL AND re.client_id = ?) as roster_count
                        FROM team_registrations tr
                        WHERE league_id = ? AND season_id = ? AND division_id = ? AND client_id = ?
                        ORDER BY team_name
                    """, (client_id, league["league_id"], season["season_id"], division["division_id"], client_id)).fetchall()
                else:
                    registrations = db.execute("""
                        SELECT registration_id, team_name, abbreviation,
                               (SELECT COUNT(*) FROM roster_entries re
                                WHERE re.registration_id = tr.registration_id AND re.removed_at IS NULL) as roster_count
                        FROM team_registrations tr
                        WHERE league_id = ? AND season_id = ? AND division_id = ?
                        ORDER BY team_name
                    """, (league["league_id"], season["season_id"], division["division_id"])).fetchall()

                division_dict["registrations"] = []

                for registration in registrations:
                    reg_dict = dict(registration)

                    # Fetch roster entries for this registration
                    if client_id:
                        roster = db.execute("""
                            SELECT re.id, re.player_id, p.full_name, re.jersey_number, re.position,
                                   re.roster_status, re.is_captain, re.is_alternate
                            FROM roster_entries re
                            JOIN players p ON re.player_id = p.player_id AND re.client_id = p.client_id
                            WHERE re.registration_id = ? AND re.removed_at IS NULL AND re.client_id = ?
                            ORDER BY re.jersey_number, p.last_name
                        """, (registration["registration_id"], client_id)).fetchall()
                    else:
                        roster = db.execute("""
                            SELECT re.id, re.player_id, p.full_name, re.jersey_number, re.position,
                                   re.roster_status, re.is_captain, re.is_alternate
                            FROM roster_entries re
                            JOIN players p ON re.player_id = p.player_id
                            WHERE re.registration_id = ? AND re.removed_at IS NULL
                            ORDER BY re.jersey_number, p.last_name
                        """, (registration["registration_id"],)).fetchall()

                    reg_dict["roster"] = [dict(r) for r in roster]
                    division_dict["registrations"].append(reg_dict)

                # Fetch games for this division
                if client_id:
                    games = db.execute("""
                        SELECT DISTINCT g.game_id, g.home_team, g.away_team, g.home_abbrev, g.away_abbrev,
                               g.start_time, g.rink_id, g.sheet_id,
                               r.name as venue_name,
                               rs.name as sheet_name,
                               g.home_registration_id, g.away_registration_id
                        FROM games g
                        LEFT JOIN team_registrations tr_home ON g.home_registration_id = tr_home.registration_id AND g.client_id = tr_home.client_id
                        LEFT JOIN team_registrations tr_away ON g.away_registration_id = tr_away.registration_id AND g.client_id = tr_away.client_id
                        LEFT JOIN rinks r ON g.rink_id = r.rink_id AND g.client_id = r.client_id
                        LEFT JOIN rink_sheets rs ON g.sheet_id = rs.sheet_id AND g.client_id = rs.client_id AND g.rink_id = rs.rink_id
                        WHERE g.client_id = ?
                          AND ((tr_home.league_id = ? AND tr_home.season_id = ? AND tr_home.division_id = ?)
                           OR (tr_away.league_id = ? AND tr_away.season_id = ? AND tr_away.division_id = ?))
                        ORDER BY g.start_time
                    """, (client_id, league["league_id"], season["season_id"], division["division_id"],
                          league["league_id"], season["season_id"], division["division_id"])).fetchall()
                else:
                    games = db.execute("""
                        SELECT DISTINCT g.game_id, g.home_team, g.away_team, g.home_abbrev, g.away_abbrev,
                               g.start_time, g.rink_id, g.sheet_id,
                               r.name as venue_name,
                               rs.name as sheet_name,
                               g.home_registration_id, g.away_registration_id
                        FROM games g
                        LEFT JOIN team_registrations tr_home ON g.home_registration_id = tr_home.registration_id
                        LEFT JOIN team_registrations tr_away ON g.away_registration_id = tr_away.registration_id
                        LEFT JOIN rinks r ON g.rink_id = r.rink_id
                        LEFT JOIN rink_sheets rs ON g.sheet_id = rs.sheet_id
                        WHERE (tr_home.league_id = ? AND tr_home.season_id = ? AND tr_home.division_id = ?)
                           OR (tr_away.league_id = ? AND tr_away.season_id = ? AND tr_away.division_id = ?)
                        ORDER BY g.start_time
                    """, (league["league_id"], season["season_id"], division["division_id"],
                          league["league_id"], season["season_id"], division["division_id"])).fetchall()

                # Reconstruct game states
                games_with_state = []
                for game in games:
                    game_dict = dict(game)
                    state = reconstruct_game_state(game["game_id"])
                    if state:
                        game_dict["state"] = state
                    games_with_state.append(game_dict)

                division_dict["games"] = games_with_state

                season_dict["divisions"].append(division_dict)

            league_dict["seasons"].append(season_dict)

        org_data.append(league_dict)

    # Fetch all players for dropdown (filtered by client)
    if client_id:
        all_players = db.execute(
            "SELECT player_id, full_name, first_name, last_name, shoots_catches FROM players WHERE client_id = ? ORDER BY last_name, first_name",
            (client_id,)
        ).fetchall()
    else:
        all_players = db.execute("SELECT player_id, full_name, first_name, last_name, shoots_catches FROM players ORDER BY last_name, first_name").fetchall()

    # Fetch all unique teams for team re-registration dropdown (includes withdrawn teams)
    if client_id:
        all_teams = db.execute("""
            SELECT DISTINCT team_name, abbreviation, organizer_name, organizer_email, organizer_phone
            FROM team_registrations
            WHERE client_id = ?
            ORDER BY team_name
        """, (client_id,)).fetchall()
    else:
        all_teams = db.execute("""
            SELECT DISTINCT team_name, abbreviation, organizer_name, organizer_email, organizer_phone
            FROM team_registrations
            ORDER BY team_name
        """).fetchall()

    # Fetch active registrations per division (to exclude from dropdown when adding to that division)
    if client_id:
        active_registrations = db.execute("""
            SELECT league_id, season_id, division_id, team_name, abbreviation
            FROM team_registrations
            WHERE withdrawn_at IS NULL AND client_id = ?
        """, (client_id,)).fetchall()
    else:
        active_registrations = db.execute("""
            SELECT league_id, season_id, division_id, team_name, abbreviation
            FROM team_registrations
            WHERE withdrawn_at IS NULL
        """).fetchall()

    # Build a map: "league_id|season_id|division_id" -> ["TeamName|ABBR", ...]
    active_teams_by_division = {}
    for reg in active_registrations:
        div_key = f"{reg['league_id']}|{reg['season_id']}|{reg['division_id']}"
        team_key = f"{reg['team_name']}|{reg['abbreviation']}"
        if div_key not in active_teams_by_division:
            active_teams_by_division[div_key] = []
        active_teams_by_division[div_key].append(team_key)

    # Fetch all rinks and sheets for schedule modal
    if client_id:
        all_rinks = db.execute("SELECT rink_id, name FROM rinks WHERE client_id = ? ORDER BY name", (client_id,)).fetchall()
        all_sheets = db.execute("SELECT sheet_id, rink_id, name FROM rink_sheets WHERE client_id = ? ORDER BY rink_id, name", (client_id,)).fetchall()
    else:
        all_rinks = db.execute("SELECT rink_id, name FROM rinks ORDER BY name").fetchall()
        all_sheets = db.execute("SELECT sheet_id, rink_id, name FROM rink_sheets ORDER BY rink_id, name").fetchall()

    db.close()

    # Generate HTML tree
    def generate_tree_html(data):
        import html
        from datetime import datetime as dt

        if not data:
            return '<div class="empty-state">No leagues found. <button class="btn-add" onclick="openModal(\'league\')">+ Add League</button></div>'

        html_parts = []
        for league in data:
            # Calculate league totals
            league_team_count = sum(len(d["registrations"]) for s in league["seasons"] for d in s["divisions"])
            league_player_count = sum(r["roster_count"] for s in league["seasons"] for d in s["divisions"] for r in d["registrations"])
            league_game_count = sum(len(d.get("games", [])) for s in league["seasons"] for d in s["divisions"])

            league_badge = f'<span class="node-badge league-{league["league_type"] or "unknown"}">{league["league_type"] or "N/A"}</span>'
            html_parts.append(f'''
            <div class="tree-node level-1" data-id="{league["league_id"]}" data-type="league">
                <div class="node-header" onclick="toggleNode(this)">
                    <span class="toggle-icon">▶</span>
                    <span class="node-name">{league["name"]}</span>
                    {league_badge}
                    <span class="node-meta">({league_team_count} teams, {league_player_count} players, {league_game_count} games)</span>
                    <button class="btn-add" onclick="event.stopPropagation(); openModal('season', '{league["league_id"]}')">+ Season</button>
                </div>
                <div class="node-children" style="display: none;">
            ''')

            for season in league["seasons"]:
                # Calculate season totals
                season_team_count = sum(len(d["registrations"]) for d in season["divisions"])
                season_player_count = sum(r["roster_count"] for d in season["divisions"] for r in d["registrations"])
                season_game_count = sum(len(d.get("games", [])) for d in season["divisions"])

                date_range = f'{season["start_date"]} to {season["end_date"] or "ongoing"}'

                # Prepare divisions data for multi-division scheduling
                divisions_json = json.dumps([{
                    "division_id": d["division_id"],
                    "name": d["name"],
                    "team_count": len(d["registrations"])
                } for d in season["divisions"]])
                # HTML-escape the JSON for safe embedding in onclick attribute
                divisions_json_escaped = html.escape(divisions_json)

                html_parts.append(f'''
                <div class="tree-node level-2" data-id="{season["season_id"]}" data-type="season">
                    <div class="node-header" onclick="toggleNode(this)">
                        <span class="toggle-icon">▶</span>
                        <span class="node-name">{season["name"]}</span>
                        <span class="node-meta">{date_range} • {season_team_count} teams, {season_game_count} games</span>
                        <button class="btn-schedule" onclick="event.stopPropagation(); openMultiDivisionScheduleModal('{league["league_id"]}', '{season["season_id"]}', '{season["name"]}', JSON.parse('{divisions_json_escaped}'))">Schedule Games</button>
                        <button class="btn-add" onclick="event.stopPropagation(); openModal('division', '{league["league_id"]}', '{season["season_id"]}')">+ Division</button>
                    </div>
                    <div class="node-children" style="display: none;">
                ''')

                for division in season["divisions"]:
                    # Calculate division totals
                    division_team_count = len(division["registrations"])
                    division_player_count = sum(r["roster_count"] for r in division["registrations"])
                    division_game_count = len(division.get("games", []))
                    div_id_safe = division["division_id"].replace("-", "_")

                    html_parts.append(f'''
                    <div class="tree-node level-3" data-id="{division["division_id"]}" data-type="division">
                        <div class="node-header" onclick="toggleNode(this)">
                            <span class="toggle-icon">▶</span>
                            <span class="node-name">{division["name"]}</span>
                            <span class="node-meta">({division_team_count} teams, {division_player_count} players, {division_game_count} games)</span>
                        </div>
                        <div class="node-children" style="display: none;">
                    ''')

                    # Teams subsection
                    html_parts.append(f'''
                        <div class="tree-node level-4" data-id="{division["division_id"]}-teams" data-type="teams-section">
                            <div class="node-header" onclick="toggleNode(this)">
                                <span class="toggle-icon">▶</span>
                                <span class="node-name">Teams</span>
                                <span class="node-meta">({division_team_count} teams)</span>
                                <button class="btn-add" onclick="event.stopPropagation(); openModal('registration', '{league["league_id"]}', '{season["season_id"]}', '{division["division_id"]}')">+ Team</button>
                            </div>
                            <div class="node-children" style="display: none;">
                    ''')

                    for reg in division["registrations"]:
                        roster_count = f'({reg["roster_count"]} players)'
                        html_parts.append(f'''
                            <div class="tree-node level-5" data-id="{reg["registration_id"]}" data-type="registration">
                                <div class="node-header" onclick="toggleNode(this)">
                                    <span class="toggle-icon">▶</span>
                                    <span class="node-name">{reg["team_name"]} ({reg["abbreviation"]})</span>
                                    <span class="node-meta">{roster_count}</span>
                                    <button class="btn-add" onclick="event.stopPropagation(); openModal('player', '{reg["registration_id"]}', '{reg["team_name"]}')">+ Player</button>
                                    <button class="btn-remove-team" onclick="event.stopPropagation(); removeTeam('{reg["registration_id"]}', '{reg["team_name"]}')" title="Remove team from division">&times;</button>
                                </div>
                                <div class="node-children" style="display: none;">
                        ''')

                        for player in reg["roster"]:
                            jersey = f'#{player["jersey_number"]}' if player["jersey_number"] else ''
                            position = player["position"] or ''
                            captain_badge = '<span class="role-badge captain">C</span>' if player["is_captain"] else ''
                            alternate_badge = '<span class="role-badge alternate">A</span>' if player["is_alternate"] else ''

                            html_parts.append(f'''
                                <div class="tree-node level-6 leaf" data-id="{player["player_id"]}" data-type="player">
                                    <div class="node-header">
                                        <span class="node-name">{jersey} {player["full_name"]} {position} {captain_badge}{alternate_badge}</span>
                                        <button class="btn-remove-player" onclick="removePlayer({player["id"]}, '{player["full_name"]}')" title="Remove from roster">&times;</button>
                                    </div>
                                </div>
                            ''')

                        if not reg["roster"]:
                            html_parts.append('<div class="empty-state-inline">No players on roster</div>')

                        html_parts.append('</div></div>')  # Close registration

                    if not division["registrations"]:
                        html_parts.append('<div class="empty-state-inline">No teams registered</div>')

                    html_parts.append('</div></div>')  # Close Teams subsection

                    # Schedule subsection
                    games = division.get("games", [])

                    # Generate team filter options
                    team_filter_options = '<option value="">All Teams</option>'
                    for team in division["registrations"]:
                        team_filter_options += f'<option value="{team["registration_id"]}">{team["team_name"]}</option>'

                    html_parts.append(f'''
                        <div class="tree-node level-4" data-id="{division["division_id"]}-schedule" data-type="schedule-section">
                            <div class="node-header" onclick="toggleNode(this)">
                                <span class="toggle-icon">▶</span>
                                <span class="node-name">Schedule</span>
                                <span class="node-meta">({division_game_count} games)</span>
                            </div>
                            <div class="node-children" style="display: none;">
                    ''')

                    if games:
                        html_parts.append(f'''
                            <div style="margin: 6px 0; display: flex; align-items: center; gap: 8px;">
                                <label style="font-size: 12px; color: #666;">Filter:</label>
                                <select onchange="filterDivisionGames('{div_id_safe}', this.value)" style="padding: 3px 6px; font-size: 12px;">
                                    {team_filter_options}
                                </select>
                            </div>
                            <table class="schedule-games-table" id="games_{div_id_safe}">
                                <thead>
                                    <tr>
                                        <th style="width: 60px;">Date</th>
                                        <th style="width: 60px;">Time</th>
                                        <th style="width: 80px;">Venue</th>
                                        <th style="width: 80px; text-align: right;">Home</th>
                                        <th style="width: 20px; text-align: center;"></th>
                                        <th style="width: 80px;">Away</th>
                                        <th style="width: 50px; text-align: center;">Score</th>
                                        <th style="width: 60px;">Status</th>
                                    </tr>
                                </thead>
                                <tbody>
                        ''')

                        for game in games:
                            # Parse start time
                            game_dt = dt.fromisoformat(game["start_time"])
                            game_date = game_dt.strftime("%b %d")
                            game_time = game_dt.strftime("%I:%M%p").lstrip("0").lower()
                            venue_str = game["sheet_name"] or game["venue_name"] or "-"

                            # Get state info
                            home_score = game.get("state", {}).get("home_score", 0)
                            away_score = game.get("state", {}).get("away_score", 0)
                            score_str = f"{home_score} - {away_score}"

                            # Determine status
                            clock_running = game.get("state", {}).get("clock_running", False)
                            if clock_running:
                                status_class = "running"
                                status_text = "Live"
                            elif home_score > 0 or away_score > 0:
                                status_class = "final"
                                status_text = "Final"
                            else:
                                status_class = "scheduled"
                                status_text = "Scheduled"

                            home_reg_id = game["home_registration_id"] or ""
                            away_reg_id = game["away_registration_id"] or ""

                            html_parts.append(f'''
                                <tr data-home-reg="{home_reg_id}" data-away-reg="{away_reg_id}">
                                    <td>{game_date}</td>
                                    <td>{game_time}</td>
                                    <td>{venue_str}</td>
                                    <td style="text-align: right;">{game["home_abbrev"] or game["home_team"]}</td>
                                    <td style="text-align: center;">vs</td>
                                    <td>{game["away_abbrev"] or game["away_team"]}</td>
                                    <td style="text-align: center; font-weight: bold;">{score_str}</td>
                                    <td><span class="status {status_class}">{status_text}</span></td>
                                </tr>
                            ''')

                        html_parts.append('</tbody></table>')
                    else:
                        html_parts.append('<div class="empty-state-inline">No games scheduled</div>')

                    html_parts.append('</div></div>')  # Close Schedule subsection

                    html_parts.append('</div></div>')  # Close division

                if not season["divisions"]:
                    html_parts.append('<div class="empty-state-inline">No divisions</div>')

                html_parts.append('</div></div>')  # Close season

            if not league["seasons"]:
                html_parts.append('<div class="empty-state-inline">No seasons</div>')

            html_parts.append('</div></div>')  # Close league

        return ''.join(html_parts)

    tree_html = generate_tree_html(org_data)

    # Generate player options for dropdown (with JSON data for auto-fill)
    import html as html_module
    player_options = ''
    player_data_json = {}
    for p in all_players:
        player_options += f'<option value="{p["player_id"]}">{p["last_name"]}, {p["first_name"]}</option>'
        player_data_json[p["player_id"]] = {
            "first_name": p["first_name"],
            "last_name": p["last_name"],
            "shoots_catches": p["shoots_catches"] or ""
        }
    player_data_json_str = json.dumps(player_data_json)

    # Generate team options for dropdown (including JSON data for auto-fill)
    import html as html_module
    team_options_html = '<option value="">-- Select Existing Team or Enter Manually --</option>'
    team_data_json = {}
    for team in all_teams:
        team_key = f"{team['team_name']}|{team['abbreviation']}"
        team_options_html += f'<option value="{html_module.escape(team_key)}">{team["team_name"]} ({team["abbreviation"]})</option>'
        team_data_json[team_key] = {
            "team_name": team["team_name"],
            "abbreviation": team["abbreviation"],
            "organizer_name": team["organizer_name"] or "",
            "organizer_email": team["organizer_email"] or "",
            "organizer_phone": team["organizer_phone"] or ""
        }
    team_data_json_str = json.dumps(team_data_json)
    active_teams_by_division_str = json.dumps(active_teams_by_division)

    # Generate sheets checkboxes for schedule modal, grouped by rink
    sheets_by_rink = {}
    for sheet in all_sheets:
        rink_id = sheet["rink_id"]
        if rink_id not in sheets_by_rink:
            sheets_by_rink[rink_id] = []
        sheets_by_rink[rink_id].append(sheet)

    sheets_html_parts = []
    for rink in all_rinks:
        rink_sheets = sheets_by_rink.get(rink["rink_id"], [])
        if rink_sheets:
            sheets_html_parts.append(f'<div style="margin-bottom: 8px;"><strong>{rink["name"]}</strong></div>')
            for sheet in rink_sheets:
                sheets_html_parts.append(f'<label style="display: block; margin-left: 16px;"><input type="checkbox" name="sheet" value="{sheet["sheet_id"]}"> {sheet["name"]}</label>')
    sheets_checkboxes_html = ''.join(sheets_html_parts) if sheets_html_parts else '<p style="color: #999;">No venues/sheets configured. Add them in Venues first.</p>'

    return templates.TemplateResponse("admin/organization.html", {
        "request": request,
        "nav_html": admin_nav("leagues", session),
        "wide": True,
        "tree_html": tree_html,
        "player_options": player_options,
        "player_data_json_str": player_data_json_str,
        "team_options_html": team_options_html,
        "team_data_json_str": team_data_json_str,
        "active_teams_by_division_str": active_teams_by_division_str,
        "sheets_checkboxes_html": sheets_checkboxes_html
    })





@app.get("/admin/players")
async def get_players_admin(request: Request, format: Optional[str] = Query(None, description="Response format: 'json' or 'html'")):
    """
    Admin page to view all players.

    Returns HTML for browser viewing or JSON if format=json parameter is provided.
    """
    from fastapi.responses import HTMLResponse

    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    db = get_db()

    # Fetch players (filtered by client)
    if client_id:
        players = db.execute("""
            SELECT player_id, first_name, last_name, created_at
            FROM players
            WHERE client_id = ?
            ORDER BY last_name, first_name
        """, (client_id,)).fetchall()
    else:
        # Super admin viewing all clients
        players = db.execute("""
            SELECT player_id, first_name, last_name, created_at
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
        players_html = '<tr><td colspan="4" style="text-align: center; color: #999; padding: 40px;">No players found.</td></tr>'
    else:
        for p in players:
            players_html += f'''
            <tr>
                <td class="player-id">{p["player_id"]}</td>
                <td>{p["first_name"] or "-"}</td>
                <td>{p["last_name"] or "-"}</td>
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
                    firstName: document.getElementById('filterFirstName').value.toLowerCase(),
                    lastName: document.getElementById('filterLastName').value.toLowerCase()
                }};

                const rows = document.querySelectorAll('#playersTable tbody tr');

                rows.forEach(row => {{
                    if (row.cells.length < 4) return; // Skip empty row

                    const playerId = row.cells[0].textContent.toLowerCase();
                    const firstName = row.cells[1].textContent.toLowerCase();
                    const lastName = row.cells[2].textContent.toLowerCase();

                    const match =
                        playerId.includes(filters.playerId) &&
                        firstName.includes(filters.firstName) &&
                        lastName.includes(filters.lastName);

                    row.style.display = match ? '' : 'none';
                }});
            }}
        </script>
    </head>
    <body>
        {admin_nav("players", session)}
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
                            <th>First Name</th>
                            <th>Last Name</th>
                            <th>Created</th>
                        </tr>
                        <tr class="filter-row">
                            <td><input type="text" id="filterPlayerId" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterFirstName" placeholder="Filter..." onkeyup="filterTable()"></td>
                            <td><input type="text" id="filterLastName" placeholder="Filter..." onkeyup="filterTable()"></td>
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
    Player, Rink, RinkSheet, Official,
    RuleSet, Infraction,
    TeamRegistration, RosterEntry,
)



@app.post("/admin/leagues")
async def create_league(request: Request, league: League):
    """Create a new league."""
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    # Super admin must specify a client context
    if not client_id:
        raise HTTPException(status_code=400, detail="Super admin must have active client selected")

    db = get_db()
    current_time = int(time.time())
    try:
        db.execute("""
            INSERT INTO leagues (client_id, league_id, name, league_type, description, website, logo_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (client_id, league.league_id, league.name, league.league_type, league.description,
              league.website, league.logo_url, current_time))
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        raise HTTPException(status_code=409, detail=f"League {league.league_id} already exists")
    db.close()
    return {"status": "ok", "message": f"League {league.league_id} created"}


@app.post("/admin/seasons")
async def create_season(request: Request, season: Season):
    """Create a new season."""
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    # Super admin must specify a client context
    if not client_id:
        raise HTTPException(status_code=400, detail="Super admin must have active client selected")

    db = get_db()
    current_time = int(time.time())
    try:
        db.execute("""
            INSERT INTO seasons (client_id, season_id, name, start_date, end_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (client_id, season.season_id, season.name, season.start_date, season.end_date, current_time))
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        raise HTTPException(status_code=409, detail=f"Season {season.season_id} already exists")
    db.close()
    return {"status": "ok", "message": f"Season {season.season_id} created"}


@app.post("/admin/league-seasons")
async def create_league_season(
    request: Request,
    league_id: str = Body(...),
    season_id: str = Body(...),
    rule_set_id: Optional[str] = Body(None)
):
    """Link a season to a league."""
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    # Super admin must specify a client context
    if not client_id:
        raise HTTPException(status_code=400, detail="Super admin must have active client selected")

    db = get_db()
    current_time = int(time.time())
    try:
        db.execute("""
            INSERT INTO league_seasons (client_id, league_id, season_id, rule_set_id, is_active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
        """, (client_id, league_id, season_id, rule_set_id, current_time))
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        raise HTTPException(status_code=409, detail=f"League {league_id} already linked to season {season_id}")
    db.close()
    return {"status": "ok", "message": f"Season {season_id} linked to league {league_id}"}


@app.post("/admin/divisions")
async def create_division(request: Request, division: Division):
    """Create a new division."""
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    # Super admin must specify a client context
    if not client_id:
        raise HTTPException(status_code=400, detail="Super admin must have active client selected")

    db = get_db()
    current_time = int(time.time())
    try:
        db.execute("""
            INSERT INTO divisions (client_id, division_id, name, division_type, parent_division_id, description, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (client_id, division.division_id, division.name, division.division_type,
              division.parent_division_id, division.description, current_time))
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        raise HTTPException(status_code=409, detail=f"Division {division.division_id} already exists")
    db.close()
    return {"status": "ok", "message": f"Division {division.division_id} created"}


@app.post("/admin/league-season-divisions")
async def link_division_to_league_season(
    request: Request,
    league_id: str = Body(...),
    season_id: str = Body(...),
    division_id: str = Body(...),
    display_order: Optional[int] = Body(None)
):
    """Link a division to a league-season."""
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    # Super admin must specify a client context
    if not client_id:
        raise HTTPException(status_code=400, detail="Super admin must have active client selected")

    db = get_db()
    current_time = int(time.time())
    try:
        db.execute("""
            INSERT INTO league_season_divisions (client_id, league_id, season_id, division_id, display_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (client_id, league_id, season_id, division_id, display_order, current_time))
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        raise HTTPException(status_code=409, detail=f"Division {division_id} already linked to league {league_id} season {season_id}")
    db.close()
    return {"status": "ok", "message": f"Division {division_id} linked to league-season"}


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


@app.post("/admin/team-registrations")
async def create_team_registration(request: Request, reg: TeamRegistration):
    """Register a team in a league+season or tournament."""
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    # Super admin must specify a client context
    if not client_id:
        raise HTTPException(status_code=400, detail="Super admin must have active client selected")

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

    # Check for duplicate active registration (same team in same division)
    if reg.league_id and reg.season_id:
        existing = db.execute("""
            SELECT registration_id FROM team_registrations
            WHERE client_id = ? AND league_id = ? AND season_id = ? AND division_id = ?
              AND (team_name = ? OR abbreviation = ?)
              AND withdrawn_at IS NULL
        """, (client_id, reg.league_id, reg.season_id, reg.division_id, reg.team_name, reg.abbreviation)).fetchone()

        if existing:
            db.close()
            raise HTTPException(
                status_code=409,
                detail=f"Team '{reg.team_name}' (or abbreviation '{reg.abbreviation}') is already registered in this division"
            )
    elif reg.tournament_id:
        existing = db.execute("""
            SELECT registration_id FROM team_registrations
            WHERE client_id = ? AND tournament_id = ? AND division_id = ?
              AND (team_name = ? OR abbreviation = ?)
              AND withdrawn_at IS NULL
        """, (client_id, reg.tournament_id, reg.division_id, reg.team_name, reg.abbreviation)).fetchone()

        if existing:
            db.close()
            raise HTTPException(
                status_code=409,
                detail=f"Team '{reg.team_name}' (or abbreviation '{reg.abbreviation}') is already registered in this division"
            )

    try:
        db.execute("""
            INSERT INTO team_registrations
                (client_id, registration_id, team_name, abbreviation, logo_url, primary_color, secondary_color,
                 organizer_name, organizer_email, organizer_phone,
                 league_id, season_id, tournament_id, division_id, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (client_id, reg.registration_id, reg.team_name, reg.abbreviation, reg.logo_url,
              reg.primary_color, reg.secondary_color, reg.organizer_name, reg.organizer_email,
              reg.organizer_phone, reg.league_id, reg.season_id, reg.tournament_id,
              reg.division_id, current_time))
        db.commit()
    except sqlite3.IntegrityError as e:
        db.close()
        raise HTTPException(status_code=409, detail=str(e))
    db.close()
    return {"status": "ok", "message": f"Team {reg.team_name} registered as {reg.registration_id}"}


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


@app.post("/admin/players")
async def create_player(request: Request, player: CreatePlayerRequest):
    """Create a new player."""
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    # Super admin must specify a client context
    if not client_id:
        raise HTTPException(status_code=400, detail="Super admin must have active client selected")

    db = get_db()
    current_time = int(time.time())
    full_name = f"{player.first_name} {player.last_name}"

    try:
        cursor = db.execute("""
            INSERT INTO players (client_id, first_name, last_name, full_name, shoots_catches, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (client_id, player.first_name, player.last_name, full_name, player.shoots_catches, current_time))
        player_id = cursor.lastrowid
        db.commit()
    except sqlite3.IntegrityError as e:
        db.close()
        raise HTTPException(status_code=409, detail=str(e))
    db.close()
    return {"status": "ok", "message": f"Player {full_name} created", "player_id": player_id}


@app.post("/admin/roster-entries")
async def add_roster_entry(request: Request, entry: RosterEntry):
    """Add a player to a team's roster."""
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    # Super admin must specify a client context
    if not client_id:
        raise HTTPException(status_code=400, detail="Super admin must have active client selected")

    db = get_db()
    current_time = int(time.time())

    # Validate that registration exists and belongs to client
    reg = db.execute("""
        SELECT client_id FROM team_registrations WHERE registration_id = ?
    """, (entry.registration_id,)).fetchone()
    if not reg:
        db.close()
        raise HTTPException(status_code=404, detail=f"Registration {entry.registration_id} not found")
    if reg["client_id"] != client_id:
        db.close()
        raise HTTPException(status_code=403, detail="Cannot add player to registration from different client")

    # Validate that player exists and belongs to client
    player = db.execute("""
        SELECT client_id FROM players WHERE client_id = ? AND player_id = ?
    """, (client_id, entry.player_id)).fetchone()
    if not player:
        db.close()
        raise HTTPException(status_code=404, detail=f"Player {entry.player_id} not found in client context")

    try:
        db.execute("""
            INSERT INTO roster_entries
                (client_id, registration_id, player_id, jersey_number, position, roster_status,
                 is_captain, is_alternate, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (client_id, entry.registration_id, entry.player_id, entry.jersey_number, entry.position,
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


@app.delete("/admin/roster-entries/{roster_entry_id}")
async def remove_roster_entry(request: Request, roster_entry_id: int):
    """Remove a player from a team roster (soft delete via removed_at timestamp)."""
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    # Super admin must specify a client context
    if not client_id:
        raise HTTPException(status_code=400, detail="Super admin must have active client selected")

    db = get_db()
    current_time = int(time.time())

    # Check if entry exists and belongs to client
    entry = db.execute("""
        SELECT re.id, re.registration_id, re.player_id, re.client_id
        FROM roster_entries re
        WHERE re.id = ?
    """, (roster_entry_id,)).fetchone()

    if not entry:
        db.close()
        raise HTTPException(status_code=404, detail=f"Roster entry {roster_entry_id} not found")

    if entry["client_id"] != client_id:
        db.close()
        raise HTTPException(status_code=403, detail="Cannot remove roster entry from different client")

    # Soft delete by setting removed_at
    db.execute(
        "UPDATE roster_entries SET removed_at = ? WHERE id = ?",
        (current_time, roster_entry_id)
    )
    db.commit()
    db.close()

    logger.info(f"Removed roster entry {roster_entry_id} (player {entry['player_id']} from {entry['registration_id']})")

    return {"status": "ok", "message": f"Player removed from roster"}


@app.delete("/admin/registrations/{registration_id}")
async def remove_team_registration(request: Request, registration_id: str):
    """Remove a team registration from a league/season/division (soft delete via withdrawn_at)."""
    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    # Super admin must specify a client context
    if not client_id:
        raise HTTPException(status_code=400, detail="Super admin must have active client selected")

    db = get_db()
    current_time = int(time.time())

    # Check if registration exists and belongs to client
    reg = db.execute("""
        SELECT registration_id, team_name, league_id, season_id, division_id, client_id
        FROM team_registrations WHERE registration_id = ?
    """, (registration_id,)).fetchone()

    if not reg:
        db.close()
        raise HTTPException(status_code=404, detail=f"Registration {registration_id} not found")

    if reg["client_id"] != client_id:
        db.close()
        raise HTTPException(status_code=403, detail="Cannot remove registration from different client")

    team_name = reg["team_name"]
    league_id = reg["league_id"]
    season_id = reg["season_id"]
    division_id = reg["division_id"]

    # Soft delete roster entries for this registration
    roster_count = db.execute(
        "UPDATE roster_entries SET removed_at = ? WHERE registration_id = ? AND removed_at IS NULL",
        (current_time, registration_id)
    ).rowcount

    # Soft delete the registration by setting withdrawn_at
    db.execute(
        "UPDATE team_registrations SET withdrawn_at = ? WHERE registration_id = ?",
        (current_time, registration_id)
    )

    db.commit()
    db.close()

    logger.info(f"Withdrew team registration {registration_id} ({team_name}) from {league_id}/{season_id}/{division_id}, soft-deleted {roster_count} roster entries")

    return {"status": "ok", "message": f"Team {team_name} withdrawn from division"}


# ---------- New HTML Admin Pages ----------

@app.get("/admin/rinks-admin")
async def list_rinks_admin(request: Request, format: Optional[str] = Query(None)):
    """List all venues with hierarchical tree view (Venues → Sheets → Devices)."""
    from fastapi.responses import HTMLResponse

    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    db = get_db()

    # Fetch all rinks (venues) filtered by client
    if client_id:
        venues = db.execute("""
            SELECT rink_id, name, address, city, province_state, postal_code, country, phone, website, created_at
            FROM rinks
            WHERE client_id = ?
            ORDER BY name
        """, (client_id,)).fetchall()
    else:
        # Super admin viewing all clients
        venues = db.execute("""
            SELECT rink_id, name, address, city, province_state, postal_code, country, phone, website, created_at
            FROM rinks ORDER BY name
        """).fetchall()

    # Build nested data structure
    venue_data = []

    for venue in venues:
        venue_dict = dict(venue)

        # Fetch sheets for this venue
        sheets = db.execute("""
            SELECT sheet_id, rink_id, name, surface_type, capacity, created_at
            FROM rink_sheets
            WHERE rink_id = ?
            ORDER BY name
        """, (venue["rink_id"],)).fetchall()

        venue_dict["sheets"] = []

        for sheet in sheets:
            sheet_dict = dict(sheet)

            # Fetch device for this sheet (1:1 relationship)
            # Devices reference sheets by rink_id + sheet_name (not sheet_id FK)
            device = db.execute("""
                SELECT device_id, device_name, last_seen_at, is_assigned
                FROM devices
                WHERE rink_id = ? AND sheet_name = ? AND is_assigned = 1
            """, (venue["rink_id"], sheet["name"])).fetchone()

            sheet_dict["device"] = dict(device) if device else None
            venue_dict["sheets"].append(sheet_dict)

        venue_data.append(venue_dict)

    # Fetch unassigned devices for this client (only recent ones)
    current_time = int(time.time())
    seven_days_ago = current_time - (7 * 24 * 60 * 60)

    if client_id:
        unassigned_devices = db.execute("""
            SELECT device_id, device_name, last_seen_at
            FROM devices
            WHERE client_id = ?
              AND (is_assigned = 0 OR is_assigned IS NULL OR rink_id IS NULL)
              AND last_seen_at > ?
            ORDER BY last_seen_at DESC
        """, (client_id, seven_days_ago)).fetchall()
    else:
        # Super admin sees all unassigned devices (only recent ones)
        unassigned_devices = db.execute("""
            SELECT device_id, device_name, last_seen_at
            FROM devices
            WHERE (is_assigned = 0 OR is_assigned IS NULL OR rink_id IS NULL)
              AND last_seen_at > ?
            ORDER BY last_seen_at DESC
        """, (seven_days_ago,)).fetchall()

    db.close()

    # Return JSON if requested
    if format == "json":
        return {
            "venues": venue_data,
            "unassigned_devices": [dict(d) for d in unassigned_devices]
        }

    # Generate dynamic HTML components
    import datetime

    def format_timestamp(ts):
        if ts:
            dt = datetime.datetime.fromtimestamp(ts)
            delta = datetime.datetime.now() - dt
            if delta.seconds < 60:
                return "Just now"
            elif delta.seconds < 3600:
                return f"{delta.seconds // 60} min ago"
            elif delta.seconds < 86400:
                return f"{delta.seconds // 3600} hr ago"
            else:
                return dt.strftime("%Y-%m-%d %H:%M")
        return "Never"

    def generate_tree_html(data):
        if not data:
            return '<div class="empty-state">No venues found. <button class="btn-add" onclick="openModal(\'venue\')">+ Add Venue</button></div>'

        html_parts = []
        for venue in data:
            sheet_count = len(venue["sheets"])
            html_parts.append(f'''
            <div class="tree-node level-1" data-id="{venue["rink_id"]}" data-type="venue">
                <div class="node-header" onclick="toggleNode(this)">
                    <span class="toggle-icon">▶</span>
                    <span class="node-name">{venue["name"]}</span>
                    <span class="node-meta">({sheet_count} sheet{"s" if sheet_count != 1 else ""})</span>
                    <button class="btn-add" onclick="event.stopPropagation(); openModal('sheet', '{venue["rink_id"]}')">+ Sheet</button>
                </div>
                <div class="node-children" style="display: none;">
            ''')

            for sheet in venue["sheets"]:
                device = sheet["device"]
                surface_badge = f'<span class="node-badge">{sheet["surface_type"]}</span>' if sheet["surface_type"] else ''

                html_parts.append(f'''
                <div class="tree-node level-2" data-id="{sheet["sheet_id"]}" data-type="sheet">
                    <div class="node-header">
                        <span class="node-name">{sheet["name"]}</span>
                        {surface_badge}
                ''')

                if device:
                    # Show device with unassign button
                    device_display = device["device_name"] if device["device_name"] else device["device_id"]
                    html_parts.append(f'''
                        <span class="node-meta">{device_display} ({format_timestamp(device["last_seen_at"])})</span>
                        <button class="btn-unassign" onclick="event.stopPropagation(); unassignDevice('{device["device_id"]}', '{venue["rink_id"]}', '{sheet["name"]}')">Unassign</button>
                    ''')
                else:
                    # Show assign device dropdown
                    html_parts.append(f'''
                        <select class="assign-select" onchange="assignDevice(this.value, '{venue["rink_id"]}', '{sheet["name"]}')" onclick="event.stopPropagation()">
                            <option value="">Assign Device...</option>
                    ''')
                    for unassigned in unassigned_devices:
                        device_label = unassigned["device_name"] if unassigned["device_name"] else unassigned["device_id"]
                        html_parts.append(f'<option value="{unassigned["device_id"]}">{device_label}</option>')
                    html_parts.append('</select>')

                # Delete button (rightmost)
                html_parts.append(f'''
                        <button class="btn-remove" onclick="event.stopPropagation(); deleteSheet('{sheet["sheet_id"]}', '{sheet["name"]}')" style="margin-left: 8px; font-size: 11px; padding: 3px 8px;">Delete</button>
                ''')

                html_parts.append('''
                    </div>
                </div>
                ''')

            if not venue["sheets"]:
                html_parts.append('<div class="empty-state-inline">No sheets</div>')

            html_parts.append('</div></div>')  # Close venue

        return ''.join(html_parts)

    tree_html = generate_tree_html(venue_data)

    # Generate unassigned devices HTML
    unassigned_html = ""
    if unassigned_devices:
        for d in unassigned_devices:
            device_display = d["device_name"] if d["device_name"] else d["device_id"]
            device_subtitle = d["device_id"] if d["device_name"] else format_timestamp(d["last_seen_at"])
            unassigned_html += f'''
            <div class="unassigned-device">
                <span class="device-id">{device_display}</span>
                <span class="device-meta">{device_subtitle}</span>
            </div>
            '''
    else:
        unassigned_html = '<div class="empty-state-inline">No unassigned devices (last 7 days)</div>'

    # Generate claim form HTML (only show if client is selected)
    if client_id:
        claim_form_html = '''
        <div class="content" style="margin-bottom: 20px;">
            <h2>Claim New Device</h2>
            <p class="hint">Enter the claim code shown on your device screen</p>
            <form method="POST" action="/admin/devices/claim" style="padding: 12px; background: #f3e5f5; border-radius: 4px; border: 1px solid #6a1b9a;">
                <div style="display: flex; gap: 10px; align-items: flex-end;">
                    <div class="form-group" style="margin: 0; flex: 0 0 200px;">
                        <label style="font-weight: 600; color: #6a1b9a;">Claim Code <span class="required">*</span></label>
                        <input type="text" name="claim_code" required placeholder="ABC-123" maxlength="7" style="text-transform: uppercase; font-family: monospace; font-size: 14px; padding: 8px 12px;">
                    </div>
                    <div class="form-group" style="margin: 0; flex: 0 0 250px;">
                        <label style="font-weight: 600; color: #6a1b9a;">Device Name (optional)</label>
                        <input type="text" name="device_name" placeholder="e.g., Sheet A Scoreboard" style="font-size: 14px; padding: 8px 12px;">
                    </div>
                    <button type="submit" class="btn-save">Claim Device</button>
                    <span style="color: #666; font-size: 12px;">After claiming, assign the device to a sheet below</span>
                </div>
            </form>
        </div>
        '''
    else:
        claim_form_html = '''
        <div class="content" style="margin-bottom: 20px; padding: 16px; background: #fff3e0; border: 1px solid #ff9800; border-radius: 4px;">
            <p style="margin: 0; color: #e65100;">
                <strong>Note:</strong> Please select a client from the dropdown above to claim devices.
            </p>
        </div>
        '''

    return templates.TemplateResponse("admin/venues.html", {
        "request": request,
        "nav_html": admin_nav("venues", session),
        "wide": True,
        "tree_html": tree_html,
        "unassigned_html": unassigned_html,
        "claim_form_html": claim_form_html
    })



# ---------- Database Seeding Admin Page ----------

from pydantic import BaseModel as PydanticBaseModel


class SeedRequest(PydanticBaseModel):
    """Request to seed database."""
    categories: list[str] = []
    player_count: int = 120
    game_count: int = 8
    seed_all: bool = False
    exclude_games: bool = False


class ClearRequest(PydanticBaseModel):
    """Request to clear database."""
    confirm: bool = False


@app.post("/admin/seed")
async def execute_seed(request: SeedRequest):
    """Execute database seeding."""
    from score.seed import (
        seed_leagues, seed_seasons, seed_divisions, seed_rinks,
        seed_players, seed_league_seasons, seed_league_season_divisions,
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
            results["players"] = seed_players(db, request.player_count)
            results["league_seasons"] = seed_league_seasons(db)
            results["league_season_divisions"] = seed_league_season_divisions(db)
            results["registrations"] = seed_registrations(db)
            results["rosters"] = seed_rosters(db)
            if not request.exclude_games:
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
            if "players" in request.categories:
                results["players"] = seed_players(db, request.player_count)
            # League seasons and league_season_divisions are implicit when seeding registrations
            if "registrations" in request.categories:
                results["league_seasons"] = seed_league_seasons(db)
                results["league_season_divisions"] = seed_league_season_divisions(db)
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


@app.delete("/admin/games/division/{league_id}/{season_id}/{division_id}")
async def delete_division_games(
    league_id: str,
    season_id: str,
    division_id: str
):
    """Delete all games for a specific division."""
    db = get_db()

    try:
        # Get all games for this division (where either home or away team is in this division)
        game_ids = db.execute("""
            SELECT DISTINCT g.game_id
            FROM games g
            LEFT JOIN team_registrations tr_home ON g.home_registration_id = tr_home.registration_id
            LEFT JOIN team_registrations tr_away ON g.away_registration_id = tr_away.registration_id
            WHERE (tr_home.league_id = ? AND tr_home.season_id = ? AND tr_home.division_id = ?)
               OR (tr_away.league_id = ? AND tr_away.season_id = ? AND tr_away.division_id = ?)
        """, (league_id, season_id, division_id, league_id, season_id, division_id)).fetchall()

        game_id_list = [row["game_id"] for row in game_ids]

        if game_id_list:
            # Delete received events for these games
            placeholders = ','.join('?' * len(game_id_list))
            db.execute(f"DELETE FROM received_events WHERE game_id IN ({placeholders})", game_id_list)

            # Delete the games
            result = db.execute(f"DELETE FROM games WHERE game_id IN ({placeholders})", game_id_list)
            count = result.rowcount
        else:
            count = 0

        db.commit()
    finally:
        db.close()

    logger.info(f"Deleted {count} games for division {division_id}")

    return {
        "status": "ok",
        "message": f"Deleted {count} games for this division"
    }


# ---------- Schedule Generation ----------

class DivisionScheduleSpec(PydanticBaseModel):
    """Specification for one division in a multi-division schedule."""
    division_id: str
    games_per_team: int

class ScheduleGenerateRequest(PydanticBaseModel):
    """Request to generate a schedule for one or more divisions."""
    league_id: str
    season_id: str
    # Single division mode (legacy) - both must be provided together or both omitted
    division_id: Optional[str] = None
    games_per_team: Optional[int] = None
    # Multi-division mode
    divisions: Optional[list[DivisionScheduleSpec]] = None
    # Common fields
    days_of_week: list[str]  # ['sunday', 'monday', etc.]
    time_slots: list[str]  # ['18:00', '19:30', '21:00']
    sheet_ids: list[str]  # Sheet IDs to use
    blackout_dates: list[str] = []  # Optional blackout dates
    clear_existing: bool = False  # Clear existing games for divisions
    # Solver weights
    weight_time_slot: int = 10
    weight_sheet: int = 10
    weight_home_away: int = 20
    weight_opponent: int = 5
    weight_packing: int = 1
    weight_no_consecutive_opponent: int = 50
    max_consecutive_byes: int = 1
    timeout_seconds: int = 30  # Max time for optimizer


@app.post("/admin/schedules/generate")
async def generate_division_schedule(request: ScheduleGenerateRequest):
    """Generate a schedule preview for one or more divisions using OR-Tools scheduler."""
    from score.scheduler import (
        ScheduleConfig, Division, Team, Sheet, SolverSettings,
        generate_schedule, analyze_fairness, _generate_slots
    )
    from datetime import datetime, time as dt_time

    db = get_db()

    try:
        # Determine if multi-division or single-division mode
        if request.divisions:
            # Multi-division mode
            division_specs = request.divisions
        elif request.division_id and request.games_per_team:
            # Single division mode (legacy)
            division_specs = [DivisionScheduleSpec(
                division_id=request.division_id,
                games_per_team=request.games_per_team
            )]
        else:
            raise HTTPException(
                status_code=400,
                detail="Must provide either 'divisions' or 'division_id' with 'games_per_team'"
            )

        # Fetch season dates
        season = db.execute(
            "SELECT start_date, end_date FROM seasons WHERE season_id = ?",
            (request.season_id,)
        ).fetchone()

        if not season:
            db.close()
            raise HTTPException(status_code=404, detail=f"Season {request.season_id} not found")

        # Build divisions list
        divisions_list = []
        for div_spec in division_specs:
            # Fetch teams in this division
            teams_rows = db.execute("""
                SELECT registration_id, team_name, abbreviation
                FROM team_registrations
                WHERE league_id = ? AND season_id = ? AND division_id = ?
            """, (request.league_id, request.season_id, div_spec.division_id)).fetchall()

            if len(teams_rows) < 2:
                db.close()
                raise HTTPException(
                    status_code=400,
                    detail=f"Need at least 2 teams in division {div_spec.division_id}. Found {len(teams_rows)}."
                )

            # Build teams
            teams = [
                Team(
                    registration_id=row["registration_id"],
                    name=row["team_name"],
                    abbreviation=row["abbreviation"],
                    division_id=div_spec.division_id
                )
                for row in teams_rows
            ]

            # Build division
            division = Division(
                division_id=div_spec.division_id,
                teams=teams,
                games_per_team=div_spec.games_per_team
            )
            divisions_list.append(division)

        # Fetch rink info from sheets
        rink_id = None
        sheets_info = []
        for sheet_id in request.sheet_ids:
            sheet_row = db.execute(
                "SELECT sheet_id, rink_id, name FROM rink_sheets WHERE sheet_id = ?",
                (sheet_id,)
            ).fetchone()
            if sheet_row:
                if rink_id is None:
                    rink_id = sheet_row["rink_id"]
                sheets_info.append(Sheet(
                    sheet_id=sheet_row["sheet_id"],
                    name=sheet_row["name"]
                ))

        if not rink_id or not sheets_info:
            db.close()
            raise HTTPException(status_code=400, detail="Invalid sheet selection")

        # Parse days of week
        day_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6
        }
        days_of_week_ints = [day_map[d] for d in request.days_of_week if d in day_map]

        # Parse time slots
        time_slots = []
        for ts in request.time_slots:
            parts = ts.split(":")
            time_slots.append(dt_time(int(parts[0]), int(parts[1])))

        # Parse blackout dates
        blackout_dates = set()
        for d_str in request.blackout_dates:
            try:
                blackout_dates.add(datetime.strptime(d_str, "%Y-%m-%d").date())
            except ValueError:
                pass  # Skip invalid dates

        # Parse season dates
        start_date = datetime.strptime(season["start_date"], "%Y-%m-%d").date()
        end_date_str = season["end_date"]
        if end_date_str:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        else:
            # Default to 6 months from start
            from datetime import timedelta
            end_date = start_date + timedelta(days=180)

        # Build config
        config = ScheduleConfig(
            league_id=request.league_id,
            season_id=request.season_id,
            rink_id=rink_id,
            sheets=sheets_info,
            divisions=divisions_list,
            period_length_min=20,  # Default
            num_periods=3,  # Default
            game_type="regular",
            days_of_week=days_of_week_ints,
            start_date=start_date,
            end_date=end_date,
            blackout_dates=blackout_dates,
            time_slots=time_slots,
            solver=SolverSettings(
                timeout_seconds=request.timeout_seconds,
                weight_time_slot=request.weight_time_slot,
                weight_sheet=request.weight_sheet,
                weight_home_away=request.weight_home_away,
                weight_opponent=request.weight_opponent,
                weight_packing=request.weight_packing,
                weight_no_consecutive_opponent=request.weight_no_consecutive_opponent,
                max_consecutive_byes=request.max_consecutive_byes,
            )
        )

    except HTTPException:
        db.close()
        raise
    except Exception as e:
        db.close()
        logger.error(f"Error preparing schedule generation: {e}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

    # Release db connection before running solver
    db.close()

    # Run the solver
    try:
        logger.info(f"Generating schedule preview for division {request.division_id}")
        games = generate_schedule(config)
        logger.info(f"Generated {len(games)} games")

        # Analyze fairness
        report = analyze_fairness(games, config)
        logger.info(f"\n{report.summary()}")

    except Exception as e:
        logger.error(f"Schedule generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Schedule generation failed: {str(e)}")

    # Convert games to JSON-serializable format for preview
    games_preview = []

    # Build a sheet_id to name mapping
    db = get_db()
    sheet_names = {}
    for sheet_id in request.sheet_ids:
        sheet_row = db.execute(
            "SELECT sheet_id, name FROM rink_sheets WHERE sheet_id = ?",
            (sheet_id,)
        ).fetchone()
        if sheet_row:
            sheet_names[sheet_row["sheet_id"]] = sheet_row["name"]
    db.close()

    for game in games:
        games_preview.append({
            "game_id": game.game_id,
            "division_id": game.division_id,
            "home_team": game.home_team,
            "away_team": game.away_team,
            "home_abbrev": game.home_abbrev,
            "away_abbrev": game.away_abbrev,
            "start_time": game.start_time.isoformat(),
            "sheet_name": sheet_names.get(game.sheet_id, "Unknown"),
            "home_registration_id": game.home_registration_id,
            "away_registration_id": game.away_registration_id,
        })

    # Build fairness metrics for frontend
    fairness_metrics = {
        "utilization_pct": round(report.utilization_pct, 1),
        "used_slots": report.used_slots,
        "total_slots": report.total_slots,
        "games_count": len(games),
        "home_away_balance": {
            team: {"home": home, "away": away}
            for team, (home, away) in report.home_away_balance.items()
        },
        "time_slot_distribution": report.time_slot_distribution,
        "sheet_distribution": report.sheet_distribution,
        "opponent_distribution": report.opponent_distribution,
    }

    # Generate all slots and build comprehensive schedule view showing used and unused slots
    all_slots = _generate_slots(config)

    # Build lookup of games by (date, time, sheet_id)
    game_lookup = {}
    for game in games:
        key = (game.start_time.date(), game.start_time.time(), game.sheet_id)
        game_lookup[key] = game

    # Find last game date to only show slots up to last scheduled game
    last_game_date = max(game.start_time.date() for game in games) if games else None

    # Build comprehensive slot list with games or "unused" markers
    comprehensive_schedule = []
    for slot in all_slots:
        # Only include slots up to the last game date
        if last_game_date and slot.date > last_game_date:
            continue

        key = (slot.date, slot.time, slot.sheet_id)
        game = game_lookup.get(key)

        if game:
            # Slot has a game
            comprehensive_schedule.append({
                "date": slot.date.isoformat(),
                "time": slot.time.strftime("%H:%M"),
                "sheet_id": slot.sheet_id,
                "sheet_name": sheet_names.get(slot.sheet_id, "Unknown"),
                "used": True,
                "game": {
                    "game_id": game.game_id,
                    "division_id": game.division_id,
                    "home_team": game.home_team,
                    "away_team": game.away_team,
                    "home_abbrev": game.home_abbrev,
                    "away_abbrev": game.away_abbrev,
                    "home_registration_id": game.home_registration_id,
                    "away_registration_id": game.away_registration_id,
                }
            })
        else:
            # Unused slot
            comprehensive_schedule.append({
                "date": slot.date.isoformat(),
                "time": slot.time.strftime("%H:%M"),
                "sheet_id": slot.sheet_id,
                "sheet_name": sheet_names.get(slot.sheet_id, "Unknown"),
                "used": False,
                "game": None
            })

    return {
        "status": "preview",
        "games": games_preview,
        "slots": comprehensive_schedule,  # Complete slot view
        "fairness": fairness_metrics,
        "config": {
            "league_id": request.league_id,
            "season_id": request.season_id,
            # For compatibility, still include division_id if single-division mode
            "division_id": request.division_id if request.division_id else None,
            "divisions": [{"division_id": d.division_id, "games_per_team": d.games_per_team}
                         for d in division_specs],
            "clear_existing": request.clear_existing,
            # Include weights so preview can show them
            "weight_time_slot": request.weight_time_slot,
            "weight_sheet": request.weight_sheet,
            "weight_home_away": request.weight_home_away,
            "weight_opponent": request.weight_opponent,
            "weight_packing": request.weight_packing,
            "weight_no_consecutive_opponent": request.weight_no_consecutive_opponent,
            "max_consecutive_byes": request.max_consecutive_byes,
            "timeout_seconds": request.timeout_seconds,
        }
    }


class SaveScheduleRequest(PydanticBaseModel):
    """Request to save a generated schedule."""
    league_id: str
    season_id: str
    division_id: str
    games: list[dict]  # Games from preview
    clear_existing: bool = False


@app.post("/admin/schedules/save")
async def save_schedule(request: SaveScheduleRequest):
    """Save a generated schedule to the database."""
    db = get_db()
    current_time = int(time.time())

    try:
        # Clear existing games if requested
        if request.clear_existing:
            game_ids = db.execute("""
                SELECT DISTINCT g.game_id
                FROM games g
                LEFT JOIN team_registrations tr_home ON g.home_registration_id = tr_home.registration_id
                LEFT JOIN team_registrations tr_away ON g.away_registration_id = tr_away.registration_id
                WHERE (tr_home.league_id = ? AND tr_home.season_id = ? AND tr_home.division_id = ?)
                   OR (tr_away.league_id = ? AND tr_away.season_id = ? AND tr_away.division_id = ?)
            """, (request.league_id, request.season_id, request.division_id,
                  request.league_id, request.season_id, request.division_id)).fetchall()

            game_id_list = [row["game_id"] for row in game_ids]
            if game_id_list:
                placeholders = ','.join('?' * len(game_id_list))
                db.execute(f"DELETE FROM received_events WHERE game_id IN ({placeholders})", game_id_list)
                db.execute(f"DELETE FROM games WHERE game_id IN ({placeholders})", game_id_list)
                logger.info(f"Cleared {len(game_id_list)} existing games for division {request.division_id}")

        # Get rink_id and sheet_ids for validation
        sheet_ids = set()
        rink_id = None
        for game in request.games:
            # Fetch sheet info to get rink_id
            sheet_row = db.execute(
                "SELECT sheet_id, rink_id FROM rink_sheets WHERE sheet_id = (SELECT sheet_id FROM rink_sheets WHERE name = ?)",
                (game["sheet_name"],)
            ).fetchone()
            if sheet_row:
                if rink_id is None:
                    rink_id = sheet_row["rink_id"]
                sheet_ids.add(sheet_row["sheet_id"])

        # Need to look up sheet_id from sheet_name
        # Build mapping
        all_sheets = db.execute("SELECT sheet_id, name FROM rink_sheets").fetchall()
        sheet_name_to_id = {row["name"]: row["sheet_id"] for row in all_sheets}

        # Insert games
        games_created = 0
        for game_data in request.games:
            sheet_id = sheet_name_to_id.get(game_data["sheet_name"])
            if not sheet_id:
                continue

            db.execute("""
                INSERT INTO games (
                    game_id, rink_id, sheet_id,
                    home_registration_id, away_registration_id,
                    home_team, away_team, home_abbrev, away_abbrev,
                    scheduled_start, start_time,
                    period_length_min, num_periods, game_type,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                game_data["game_id"],
                rink_id,
                sheet_id,
                game_data["home_registration_id"],
                game_data["away_registration_id"],
                game_data["home_team"],
                game_data["away_team"],
                game_data["home_abbrev"],
                game_data["away_abbrev"],
                game_data["start_time"],
                game_data["start_time"],
                20,  # period_length_min
                3,   # num_periods
                "regular",  # game_type
                current_time
            ))
            games_created += 1

        db.commit()
        logger.info(f"Saved {games_created} games to database for division {request.division_id}")

    except Exception as e:
        db.close()
        logger.error(f"Error saving schedule: {e}")
        raise HTTPException(status_code=500, detail=f"Error saving schedule: {str(e)}")
    finally:
        db.close()

    return {
        "status": "ok",
        "games_created": games_created
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


def query_top_scorers(db, league_id=None, season_id=None, division_id=None, final_only=True, limit=20, client_id=None):
    """Query top goal scorers from events, properly filtered by team context."""

    # Build WHERE clause for team registration filters
    filter_conditions = []
    if league_id:
        filter_conditions.append(f"tr.league_id = '{league_id}'")
    if season_id:
        filter_conditions.append(f"tr.season_id = '{season_id}'")
    if division_id:
        filter_conditions.append(f"tr.division_id = '{division_id}'")
    if client_id:
        filter_conditions.append(f"g.client_id = '{client_id}'")

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


def query_top_assists(db, league_id=None, season_id=None, division_id=None, final_only=True, limit=20, client_id=None):
    """Query top assist leaders from events, properly filtered by team context."""

    # Build WHERE clause for team registration filters
    filter_conditions = []
    if league_id:
        filter_conditions.append(f"tr.league_id = '{league_id}'")
    if season_id:
        filter_conditions.append(f"tr.season_id = '{season_id}'")
    if division_id:
        filter_conditions.append(f"tr.division_id = '{division_id}'")
    if client_id:
        filter_conditions.append(f"g.client_id = '{client_id}'")

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


def query_top_points(db, league_id=None, season_id=None, division_id=None, final_only=True, limit=20, client_id=None):
    """Query top point leaders (goals + assists) from events, properly filtered by team context."""

    # Build WHERE clause for team registration filters
    filter_conditions = []
    if league_id:
        filter_conditions.append(f"tr.league_id = '{league_id}'")
    if season_id:
        filter_conditions.append(f"tr.season_id = '{season_id}'")
    if division_id:
        filter_conditions.append(f"tr.division_id = '{division_id}'")
    if client_id:
        filter_conditions.append(f"g.client_id = '{client_id}'")

    team_filter = f"AND ({' AND '.join(filter_conditions)})" if filter_conditions else ""

    # Combine goals and assists in a single query
    query = f"""
        SELECT
            all_points.player_id,
            p.full_name,
            MAX(re.jersey_number) as jersey_number,
            MAX(tr.abbreviation) as team_abbrev,
            MAX(l.name) as league_name,
            MAX(s.name) as season_name,
            MAX(d.name) as division_name,
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
        LEFT JOIN leagues l ON tr.league_id = l.league_id
        LEFT JOIN seasons s ON tr.season_id = s.season_id
        LEFT JOIN divisions d ON tr.division_id = d.division_id
        LEFT JOIN roster_entries re ON tr.registration_id = re.registration_id
            AND all_points.player_id = re.player_id
            AND re.removed_at IS NULL
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
    request: Request,
    league_id: Optional[str] = Query(None),
    season_id: Optional[str] = Query(None),
    division_id: Optional[str] = Query(None),
    final_only: bool = Query(True),
    format: Optional[str] = Query(None)
):
    """Statistics leaderboards page."""
    from fastapi.responses import HTMLResponse

    # Require authentication
    session = require_auth(request)
    client_id = auth.get_current_client(session)

    db = get_db()

    # Query player stats (with client filtering)
    scorers = query_top_scorers(db, league_id, season_id, division_id, final_only, client_id=client_id)
    assists_leaders = query_top_assists(db, league_id, season_id, division_id, final_only, client_id=client_id)
    points_leaders = query_top_points(db, league_id, season_id, division_id, final_only, client_id=client_id)
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

    return templates.TemplateResponse("admin/stats.html", {
        "request": request,
        "nav_html": admin_nav("stats", session),
        "wide": True,
        "points_leaders": points_leaders
    })


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
