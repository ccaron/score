import asyncio
import json
import logging
import logging.handlers
import multiprocessing
import os
import time
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, WebSocket, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from score.db import get_db as _get_db, init_db as _init_db

# Set up logger for this module
logger = logging.getLogger("score.app")

# Templates directory
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ---------- Configuration ----------
from score.config import AppConfig
from score.device import get_device_id, format_device_id_for_display

# ---------- SQLite setup ----------
DB_PATH = AppConfig.DB_PATH
CLOUD_API_URL = AppConfig.CLOUD_API_URL

# Device identification - will be populated from cloud config
DEVICE_ID = get_device_id(persist_path=AppConfig.DEVICE_ID_PATH)
RINK_ID = AppConfig.RINK_ID  # Fallback, will be overridden by cloud config
DEVICE_CONFIG = None  # Will hold full device config from cloud


def fetch_device_config():
    """
    Fetch device configuration from cloud API.

    Returns device config including rink_id assignment.
    Falls back to env var RINK_ID if cloud is unavailable.
    """
    global DEVICE_CONFIG, RINK_ID

    logger.info(f"Fetching config for device: {DEVICE_ID}")

    try:
        response = requests.get(
            f"{CLOUD_API_URL}/v1/devices/{DEVICE_ID}/config",
            timeout=10
        )
        response.raise_for_status()
        config = response.json()

        DEVICE_CONFIG = config
        logger.info(f"Device config: {config}")

        if config.get("is_assigned"):
            # Use rink_id from cloud
            RINK_ID = config["rink_id"]
            logger.info(f"Device assigned to rink: {RINK_ID}, sheet: {config.get('sheet_name')}")
        else:
            # Device not assigned yet
            logger.warning(f"Device {DEVICE_ID} is not assigned to a rink yet")
            logger.warning(f"Message from cloud: {config.get('message')}")
            # Keep using fallback RINK_ID from env var

        return config

    except requests.exceptions.RequestException as e:
        # Use warning level since this is expected if cloud isn't ready yet
        logger.warning(f"Could not connect to cloud API: {type(e).__name__}")
        logger.debug(f"Connection error details: {e}")
        return None

def get_db():
    """Get database connection for app database."""
    return _get_db(DB_PATH)


def init_db():
    """Initialize app database."""
    _init_db(DB_PATH)

init_db()

# ---------- Game state ----------
class GameState:
    def __init__(self):
        self.seconds = 20 * 60
        self.running = False
        self.last_update = int(time.time())
        self.clients: list[WebSocket] = []
        self.pusher_status = "unknown"  # "healthy", "pending", "dead", "unknown"
        self.assignment_status = "unknown"  # "healthy", "pending", "unknown"
        self.schedule_status = "unknown"  # "healthy", "pending", "dead", "unknown"
        self.mode = "clock"  # "clock" or game_id
        self.current_game: Optional[dict] = None  # Current game metadata (if mode is a game_id)
        self.home_score = 0
        self.away_score = 0
        self.goals: list[dict] = []  # List of goals: {id, team, time, cancelled}
        self.home_shots = 0
        self.away_shots = 0
        # Roster state
        self.home_roster = []        # List of player_ids
        self.away_roster = []        # List of player_ids
        self.roster_details = {}     # Map: player_id -> player info dict
        self.roster_loaded = False   # Flag for roster availability

    def add_event(self, event_type, payload=None):
        # Determine game_id: use mode if it's a game, otherwise None (for clock mode)
        game_id = self.mode if self.mode != "clock" else None
        logger.debug(f"Adding event: {event_type} (game_id={game_id}) with payload: {payload}")
        db = get_db()
        db.execute(
            "INSERT INTO events (type, game_id, payload, created_at) VALUES (?, ?, ?, ?)",
            (event_type, game_id, json.dumps(payload or {}), int(time.time()))
        )
        db.commit()
        db.close()

    def has_undelivered_events(self, destination=None):
        """Check if there are any undelivered events for the given destination."""
        if destination is None:
            destination = f"cloud:{CLOUD_API_URL}"
        db = get_db()
        count = db.execute("""
            SELECT COUNT(*) FROM events e
            LEFT JOIN deliveries d ON e.id = d.event_id AND d.destination = ?
            WHERE d.event_id IS NULL OR d.delivered IN (0, 2)
        """, (destination,)).fetchone()[0]
        db.close()
        return count > 0

    def to_dict(self):
        result = {
            "seconds": self.seconds,
            "running": self.running,
            "pusher_status": self.pusher_status,
            "assignment_status": self.assignment_status,
            "schedule_status": self.schedule_status,
            "mode": self.mode,
            "current_time": time.strftime("%H:%M"),
            "device_id": format_device_id_for_display(DEVICE_ID),
            "device_assigned": DEVICE_CONFIG.get("is_assigned") if DEVICE_CONFIG else False,
            "sheet_name": DEVICE_CONFIG.get("sheet_name") if DEVICE_CONFIG else None,
            "home_score": self.home_score,
            "away_score": self.away_score,
            "goals": self.goals,
            "home_shots": self.home_shots,
            "away_shots": self.away_shots,
            "home_roster": self.home_roster,
            "away_roster": self.away_roster,
            "roster_details": self.roster_details,
            "roster_loaded": self.roster_loaded,
        }
        if self.current_game:
            result["current_game"] = self.current_game
        return result

state = GameState()

# Global reference to cloud push process for health checks
pusher_process = None


# ---------- Cloud API Client ----------
def fetch_games_from_cloud():
    """Fetch today's games from the score-cloud API."""
    try:
        response = requests.get(
            f"{CLOUD_API_URL}/v1/rinks/{RINK_ID}/schedule",
            timeout=5
        )
        response.raise_for_status()
        data = response.json()
        games = data.get("games", [])
        logger.info(f"Fetched {len(games)} games from cloud API")

        # Only update schedule status if device is assigned
        if DEVICE_CONFIG and DEVICE_CONFIG.get("is_assigned"):
            if games:
                state.schedule_status = "healthy"
            else:
                state.schedule_status = "dead"
        else:
            state.schedule_status = "unknown"

        return games
    except Exception as e:
        logger.warning(f"Failed to fetch games from cloud API: {e}")
        # Only set to "dead" if device is assigned (otherwise keep "unknown")
        if DEVICE_CONFIG and DEVICE_CONFIG.get("is_assigned"):
            state.schedule_status = "dead"
        else:
            state.schedule_status = "unknown"
        return []

def fetch_and_initialize_roster(game_id: str):
    """
    Fetch roster from cloud and create ROSTER_INITIALIZED events.

    This should be called when switching to a game mode.
    Returns True if successful, False otherwise.
    """
    try:
        response = requests.get(
            f"{CLOUD_API_URL}/v1/games/{game_id}/roster",
            timeout=5
        )
        response.raise_for_status()
        roster_data = response.json()

        # Create ROSTER_INITIALIZED event for home team
        home_players = []
        for player_id in roster_data["home_roster"]:
            player_info = roster_data["players"].get(str(player_id), {})
            home_players.append({
                "player_id": player_id,
                "full_name": player_info.get("full_name", "Unknown"),
                "jersey_number": player_info.get("jersey_number"),
                "position": player_info.get("position"),
                "status": "active"
            })

        if home_players:
            state.add_event("ROSTER_INITIALIZED", {
                "team": "home",
                "players": home_players
            })

        # Create ROSTER_INITIALIZED event for away team
        away_players = []
        for player_id in roster_data["away_roster"]:
            player_info = roster_data["players"].get(str(player_id), {})
            away_players.append({
                "player_id": player_id,
                "full_name": player_info.get("full_name", "Unknown"),
                "jersey_number": player_info.get("jersey_number"),
                "position": player_info.get("position"),
                "status": "active"
            })

        if away_players:
            state.add_event("ROSTER_INITIALIZED", {
                "team": "away",
                "players": away_players
            })

        logger.info(f"Roster initialized: {len(home_players)} home, {len(away_players)} away")
        return True

    except Exception as e:
        logger.warning(f"Failed to fetch roster for {game_id}: {e}")
        return False

# ---------- State replay ----------
def load_state_from_events():
    """Load state from events - used on startup (defaults to clock mode)."""
    logger.info("Loading state from events...")
    db = get_db()
    rows = db.execute(
        "SELECT type, game_id, payload, created_at FROM events ORDER BY created_at ASC"
    ).fetchall()
    db.close()

    # App always starts in clock mode
    logger.info(f"Found {len(rows)} total events across all games")
    logger.info(f"Starting in clock mode (default)")

    # Note: Individual game states will be loaded when switching to that game


def load_game_state(game_id: str):
    """Load state for a specific game by replaying its events."""
    from score.state import load_game_state_from_db
    import time

    logger.info(f"Loading state for game {game_id}...")

    result = load_game_state_from_db(DB_PATH, game_id)

    # If game is running, calculate elapsed time since last event for display
    if result["running"]:
        current_time = int(time.time())
        elapsed = current_time - result["last_update"]
        result["seconds"] = max(0, result["seconds"] - elapsed)
        logger.debug(f"Game is running - adjusted for {elapsed}s elapsed since last event")

    # Update global state with replayed values
    state.seconds = result["seconds"]
    state.running = result["running"]
    state.last_update = result["last_update"]
    state.home_score = result.get("home_score", 0)
    state.away_score = result.get("away_score", 0)
    state.goals = result.get("goals", [])
    state.home_shots = result.get("home_shots", 0)
    state.away_shots = result.get("away_shots", 0)
    # Load roster state
    state.home_roster = result.get("home_roster", [])
    state.away_roster = result.get("away_roster", [])
    state.roster_details = result.get("roster_details", {})
    state.roster_loaded = bool(state.home_roster or state.away_roster)

    logger.info(f"Game state loaded: {state.seconds}s, running={state.running}, score={state.home_score}-{state.away_score}, goals={len(state.goals)}, shots={state.home_shots}-{state.away_shots}, roster_loaded={state.roster_loaded}")
    return result["num_events"]

# ---------- Broadcast ----------
async def broadcast_state():
    state_dict = state.to_dict()
    logger.debug(f"Broadcasting state: mode={state_dict['mode']}, scores={state_dict['home_score']}-{state_dict['away_score']}")
    data = json.dumps({"state": state_dict})
    dead = []

    for ws in state.clients:
        try:
            await ws.send_text(data)
        except:
            dead.append(ws)

    for ws in dead:
        state.clients.remove(ws)

    if dead:
        logger.debug(f"Removed {len(dead)} disconnected client(s)")

# ---------- Game loop ----------
async def game_loop():
    last_config_check = 0
    last_games_check = -60  # Start negative so first check happens immediately
    config_check_interval = 30  # Check every 30 seconds if unassigned
    games_check_interval = 60  # Check for games every 60 seconds

    while True:
        # Check device assignment status
        if DEVICE_CONFIG is None:
            state.assignment_status = "pending"  # Still trying to connect to cloud
        elif DEVICE_CONFIG.get("is_assigned"):
            state.assignment_status = "healthy"  # Assigned
        else:
            state.assignment_status = "pending"  # Registered but not assigned

        # Check schedule status (are games available for today?)
        current_time = int(time.time())
        if current_time - last_games_check >= games_check_interval:
            last_games_check = current_time

            # Only check if device is assigned
            if DEVICE_CONFIG and DEVICE_CONFIG.get("is_assigned"):
                try:
                    games = fetch_games_from_cloud()
                    if games:
                        state.schedule_status = "healthy"  # Games available
                    else:
                        state.schedule_status = "dead"  # No games for today
                except Exception as e:
                    logger.debug(f"Failed to check games: {e}")
                    state.schedule_status = "dead"  # Failed to fetch
            else:
                state.schedule_status = "unknown"  # Not assigned yet

        # Check cloud push health and delivery status
        if pusher_process is not None:
            is_alive = pusher_process.is_alive()
            if not is_alive:
                state.pusher_status = "dead"
            elif state.has_undelivered_events():
                state.pusher_status = "pending"
            else:
                state.pusher_status = "healthy"
        else:
            state.pusher_status = "unknown"

        # Periodically retry fetching device config if unassigned
        if current_time - last_config_check >= config_check_interval:
            last_config_check = current_time

            # Retry if config is None or device is not assigned
            if DEVICE_CONFIG is None or not DEVICE_CONFIG.get("is_assigned"):
                logger.debug("Device unassigned, retrying config fetch...")
                new_config = fetch_device_config()

                # If config changed (e.g., device was just assigned), broadcast immediately
                if new_config and new_config.get("is_assigned"):
                    logger.info("Device config updated - device is now assigned!")
                    await broadcast_state()

        if state.running and state.seconds > 0:
            state.seconds -= 1
            state.last_update = int(time.time())
            await broadcast_state()
        else:
            # Even if not running, broadcast occasionally to update cloud push status
            await broadcast_state()

        await asyncio.sleep(1)

# ---------- Lifespan ----------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Starting application...")
    logger.info(f"Device ID: {DEVICE_ID}")

    # Fetch device configuration from cloud
    config = fetch_device_config()
    if config is None:
        logger.warning("Cloud API not available - will retry automatically every 30 seconds")
        logger.info(f"Using fallback rink: {RINK_ID}")
    elif not config.get("is_assigned"):
        logger.info("Device registered but not assigned - will check for assignment every 30 seconds")

    load_state_from_events()

    # Log available endpoints
    logger.info("Available endpoints:")
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if methods and path:
            methods_str = ", ".join(sorted(methods - {"HEAD", "OPTIONS"}))
            if methods_str:  # Skip if only HEAD/OPTIONS
                logger.info(f"  {methods_str:20s} {path}")

    task = asyncio.create_task(game_loop())
    logger.info("Application started")
    try:
        yield
    finally:
        logger.info("Application shutting down")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

app = FastAPI(lifespan=lifespan)

# ---------- Routes ----------
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("app/scoreboard.html", {"request": request})

@app.post("/start")
async def start_game():
    if not state.running:
        logger.info("Starting game")
        state.running = True
        state.last_update = int(time.time())
        state.add_event("GAME_STARTED")
        await broadcast_state()
    return {"status": "ok"}

@app.post("/pause")
async def pause_game():
    if state.running:
        logger.info(f"Pausing game at {state.seconds}s")
        state.running = False
        state.add_event("GAME_PAUSED")
        await broadcast_state()
    return {"status": "ok"}

@app.post("/set_time")
async def set_time(request: dict):
    time_str = request.get("time_str", "20:00")
    mins, secs = map(int, time_str.split(":"))
    new_seconds = mins * 60 + secs
    logger.info(f"Setting clock to {time_str} ({new_seconds}s)")
    state.seconds = new_seconds
    state.last_update = int(time.time())
    state.add_event("CLOCK_SET", {"seconds": state.seconds})
    await broadcast_state()
    return {"status": "ok"}

@app.post("/add_goal")
async def add_goal(request: dict):
    """Add a goal for a team."""
    team = request.get("team")  # "home" or "away"
    scorer_id = request.get("scorer_id")  # player_id or None
    assist1_id = request.get("assist1_id")  # player_id or None
    assist2_id = request.get("assist2_id")  # player_id or None

    if state.mode == "clock":
        return {"status": "error", "message": "Cannot add goal in clock mode"}

    if team not in ["home", "away"]:
        return {"status": "error", "message": "Invalid team"}

    # Generate unique ID for this goal
    import uuid
    goal_id = str(uuid.uuid4())[:8]

    # Format current game clock time
    mins = state.seconds // 60
    secs = state.seconds % 60
    game_time = f"{mins}:{secs:02d}"

    # Update score
    if team == "home":
        event_type = "GOAL_HOME"
        state.home_score += 1
        logger.info(f"Home goal scored at {game_time}, score now {state.home_score}")
    else:
        event_type = "GOAL_AWAY"
        state.away_score += 1
        logger.info(f"Away goal scored at {game_time}, score now {state.away_score}")

    # Add goal to list
    goal = {
        "id": goal_id,
        "team": team,
        "time": game_time,
        "cancelled": False,
        # Add player IDs (store as strings for consistency)
        "scorer_id": str(scorer_id) if scorer_id else None,
        "assist1_id": str(assist1_id) if assist1_id else None,
        "assist2_id": str(assist2_id) if assist2_id else None,
    }
    state.goals.append(goal)

    # Store event with goal metadata
    payload = {
        "goal_id": goal_id,
        "value": 1,
        "time": game_time,
        # Include player IDs in event payload
        "scorer_id": str(scorer_id) if scorer_id else None,
        "assist1_id": str(assist1_id) if assist1_id else None,
        "assist2_id": str(assist2_id) if assist2_id else None,
    }
    state.add_event(event_type, payload)

    await broadcast_state()
    return {"status": "ok", "goal": goal}


@app.post("/cancel_goal")
async def cancel_goal(request: dict):
    """Cancel a specific goal."""
    goal_id = request.get("goal_id")

    if state.mode == "clock":
        return {"status": "error", "message": "Cannot cancel goal in clock mode"}

    # Find the goal
    goal = next((g for g in state.goals if g["id"] == goal_id), None)
    if not goal:
        return {"status": "error", "message": "Goal not found"}

    if goal["cancelled"]:
        return {"status": "error", "message": "Goal already cancelled"}

    # Mark as cancelled
    goal["cancelled"] = True

    # Update score
    team = goal["team"]
    if team == "home":
        event_type = "GOAL_HOME"
        state.home_score = max(0, state.home_score - 1)
        logger.info(f"Home goal cancelled, score now {state.home_score}")
    else:
        event_type = "GOAL_AWAY"
        state.away_score = max(0, state.away_score - 1)
        logger.info(f"Away goal cancelled, score now {state.away_score}")

    # Store cancellation event with same metadata as original goal
    payload = {
        "goal_id": goal_id,
        "value": -1,
        "time": goal["time"],
        # Include player IDs so assists can be properly subtracted
        "scorer_id": goal.get("scorer_id"),
        "assist1_id": goal.get("assist1_id"),
        "assist2_id": goal.get("assist2_id"),
    }
    state.add_event(event_type, payload)

    await broadcast_state()
    return {"status": "ok", "goal": goal}


@app.post("/add_shot")
async def add_shot(request: dict):
    """Add a shot for a team (anonymous - no player tracking)."""
    team = request.get("team")  # "home" or "away"

    if state.mode == "clock":
        return {"status": "error", "message": "Cannot add shot in clock mode"}

    if team not in ["home", "away"]:
        return {"status": "error", "message": "Invalid team"}

    # Update shot count
    if team == "home":
        state.home_shots += 1
        event_type = "SHOT_HOME"
        logger.info(f"Home shot recorded, total shots now {state.home_shots}")
    else:
        state.away_shots += 1
        event_type = "SHOT_AWAY"
        logger.info(f"Away shot recorded, total shots now {state.away_shots}")

    # Store event (anonymous - no payload needed)
    state.add_event(event_type, {})

    await broadcast_state()
    return {"status": "ok", "team": team, "shots": state.home_shots if team == "home" else state.away_shots}


@app.post("/change_score")
async def change_score(request: dict):
    """Change the score for a team (home or away)."""
    team = request.get("team")  # "home" or "away"
    delta = request.get("delta", 0)  # +1 or -1

    if state.mode == "clock":
        return {"status": "error", "message": "Cannot change score in clock mode"}

    # Create a GOAL event with value +1 (goal scored) or -1 (goal cancelled)
    goal_value = 1 if delta > 0 else -1

    if team == "home":
        event_type = "GOAL_HOME"
        state.home_score = max(0, state.home_score + goal_value)
        logger.info(f"Home goal {'scored' if goal_value > 0 else 'cancelled'}, score now {state.home_score}")
    elif team == "away":
        event_type = "GOAL_AWAY"
        state.away_score = max(0, state.away_score + goal_value)
        logger.info(f"Away goal {'scored' if goal_value > 0 else 'cancelled'}, score now {state.away_score}")
    else:
        return {"status": "error", "message": "Invalid team"}

    # Store the goal event with metadata
    # Note: For cancellations (value=-1), include same player/assist info as original goal
    # so stats can be properly decremented
    payload = {
        "value": goal_value,
        # Future fields for goal tracking:
        # "player": "Smith",           # Required for stats
        # "assist1": "Jones",          # Required for stats
        # "assist2": "Brown",          # Required for stats
        # "time": "15:34",             # Game time when scored
        # "penalty_shot": False,
        # "empty_net": False,
        # "period": 2,
    }
    state.add_event(event_type, payload)

    await broadcast_state()
    return {"status": "ok", "home_score": state.home_score, "away_score": state.away_score}

@app.get("/games")
async def get_games():
    """Get available games from the cloud API."""
    # Only fetch games if device is assigned
    if not DEVICE_CONFIG or not DEVICE_CONFIG.get("is_assigned"):
        return {"games": []}
    games = fetch_games_from_cloud()
    return {"games": games}


@app.get("/games/{game_id}/roster")
async def get_roster(game_id: str):
    """Get roster for a game from the cloud API."""
    try:
        response = requests.get(
            f"{CLOUD_API_URL}/v1/games/{game_id}/roster",
            timeout=5
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch roster for {game_id}: {e}")
        raise HTTPException(status_code=503, detail="Cloud unavailable")


@app.post("/select_mode")
async def select_mode(request: dict):
    """Select a mode (clock or a specific game)."""
    new_mode = request.get("mode", "clock")

    logger.info(f"Selecting mode: {new_mode}")

    # If we're currently in a game and it's running, pause it first to save state
    if state.mode != "clock" and state.mode != new_mode and state.running:
        logger.info(f"Auto-pausing current game {state.mode} before switching")
        state.running = False
        state.add_event("GAME_PAUSED")

    if new_mode == "clock":
        # Switch to clock mode
        state.mode = "clock"
        state.current_game = None
        state.running = False
        state.home_score = 0
        state.away_score = 0
        state.goals = []
        state.home_shots = 0
        state.away_shots = 0
        # Clear roster state
        state.home_roster = []
        state.away_roster = []
        state.roster_details = {}
        state.roster_loaded = False
        logger.info("Switched to clock mode")
    else:
        # Switch to a game mode - fetch game details
        games = fetch_games_from_cloud()
        logger.info(f"Fetched {len(games)} games from cloud API, looking for {new_mode}")
        logger.debug(f"Available games: {[g['game_id'] for g in games]}")

        selected_game = next((g for g in games if g["game_id"] == new_mode), None)

        if selected_game:
            # First update mode and game metadata
            state.mode = new_mode
            state.current_game = selected_game
            logger.info(f"Successfully switched to game mode: {new_mode}")

            # Replay all events for this game to restore its state
            num_events = load_game_state(new_mode)

            # If no events were found for this game, initialize with default period length and scores
            if num_events == 0:
                state.seconds = selected_game["period_length_min"] * 60
                state.last_update = int(time.time())
                state.home_score = 0
                state.away_score = 0
                state.goals = []
                # Create CLOCK_SET event to record the initial state
                state.add_event("CLOCK_SET", {"seconds": state.seconds})
                logger.info(f"No prior state found, initializing game with {state.seconds}s and 0-0 score")

            # Download roster if not already loaded
            if not state.roster_loaded:
                logger.info(f"Roster not loaded, fetching from cloud...")
                success = fetch_and_initialize_roster(new_mode)
                if success:
                    # Reload state to pick up roster events
                    load_game_state(new_mode)
                else:
                    logger.warning("Roster download failed - goals will be anonymous")

            logger.info(f"Selected game: {selected_game['home_team']} vs {selected_game['away_team']}")
            logger.info(f"Game state after load: {state.home_score}-{state.away_score}, {len(state.goals)} goals")
        else:
            logger.warning(f"Game {new_mode} not found in available games, switching to clock mode")
            logger.warning(f"Available game IDs were: {[g['game_id'] for g in games]}")
            state.mode = "clock"
            state.current_game = None
            state.running = False
            state.home_score = 0
            state.away_score = 0
            state.goals = []
            state.home_shots = 0
            state.away_shots = 0

    await broadcast_state()
    return {"status": "ok", "mode": state.mode}

@app.post("/debug_events")
async def debug_events():
    logger.info("Debug events requested")
    db = get_db()
    rows = db.execute(
        "SELECT * FROM events ORDER BY created_at ASC"
    ).fetchall()
    db.close()

    print("\n===== DEBUG EVENTS =====")
    for r in rows:
        game_id_str = r['game_id'] if r['game_id'] else 'None'
        print(
            f"{r['id']:03d} | {r['type']:<15} | game:{game_id_str:<15} | "
            f"{r['payload']:<30} | {time.ctime(r['created_at'])}"
        )
    print("========================\n")

    return {"status": "events printed"}

# ---------- WebSocket ----------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.clients.append(ws)
    logger.info(f"WebSocket client connected (total: {len(state.clients)})")

    await ws.send_text(json.dumps({"state": state.to_dict()}))

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        if ws in state.clients:
            state.clients.remove(ws)
        logger.info(f"WebSocket client disconnected (total: {len(state.clients)})")

def main():
    global pusher_process

    # Suppress harmless multiprocessing semaphore warnings on shutdown
    warnings.filterwarnings("ignore", ".*resource_tracker.*", UserWarning)

    # Configure logging first - this will handle all log records
    from score.log import init_logging
    init_logging("app", color="dim cyan")

    logger.info("Starting Game Clock application")

    # Create a queue for the child process to send log records
    log_queue = multiprocessing.Queue()

    # Create a listener to process log records from the queue
    queue_listener = logging.handlers.QueueListener(
        log_queue,
        *logging.getLogger().handlers,  # Use the handlers from root logger
        respect_handler_level=True
    )
    queue_listener.start()
    logger.info("Log queue listener started")

    # Start cloud push worker in a separate process
    pusher_process = multiprocessing.Process(
        target=push_events,
        args=(log_queue,),
        name="CloudPush"
    )
    pusher_process.start()
    logger.info(f"Cloud push process started (PID: {pusher_process.pid})")

    logger.info(f"Starting web server on http://{AppConfig.HOST}:{AppConfig.PORT}")

    try:
        # Run uvicorn directly (blocking call)
        # Bind to 0.0.0.0 so it's accessible from outside the container
        uvicorn.run(app, host=AppConfig.HOST, port=AppConfig.PORT, log_config=None)
    finally:
        logger.info("Server stopped, waiting for cloud push to finish")

        # The cloud push worker should have received SIGTERM from the shell's trap
        # Just wait for it to exit gracefully
        pusher_process.join(timeout=5)

        # Force kill only if it's still alive after timeout
        if pusher_process.is_alive():
            logger.warning("Cloud push did not exit, forcing termination...")
            pusher_process.terminate()
            pusher_process.join(timeout=2)

            if pusher_process.is_alive():
                pusher_process.kill()
                pusher_process.join()

        # Give the queue listener a moment to process any remaining log messages
        time.sleep(0.2)

        # Stop the queue listener (this drains remaining items)
        queue_listener.stop()

        # Cancel the join thread to avoid blocking, then close the queue
        log_queue.cancel_join_thread()
        log_queue.close()

        logger.info("Shutdown complete")


def push_events(log_queue):
    """
    Start the cloud push worker process.

    Args:
        log_queue: multiprocessing.Queue for sending log records to main process
    """
    from score.pusher import CloudEventPusher
    from score.device import get_device_id

    # Configure logging to send records to the queue
    queue_handler = logging.handlers.QueueHandler(log_queue)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(queue_handler)

    # Get device ID (will read from persisted file)
    device_id = get_device_id(persist_path=AppConfig.DEVICE_ID_PATH)

    pusher = CloudEventPusher(
        db_path=DB_PATH,
        cloud_api_url=CLOUD_API_URL,
        device_id=device_id
    )

    try:
        pusher.run()
    except KeyboardInterrupt:
        logging.info("Shutting down gracefully...")


# ---------- Run ----------
if __name__ == "__main__":
    main()

