import asyncio
import json
import logging
import logging.handlers
import multiprocessing
import time
import sqlite3
import warnings
from contextlib import asynccontextmanager
from typing import Optional

import requests
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import uvicorn

# Set up logger for this module
logger = logging.getLogger("score.app")

# ---------- Inline HTML + JS ----------
html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Game Clock</title>
<style>
* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    color: #fff;
}

.clock {
    font-size: 8em;
    font-weight: 700;
    margin: 0.5em;
    cursor: pointer;
    user-select: none;
    background: rgba(255, 255, 255, 0.1);
    backdrop-filter: blur(10px);
    padding: 0.3em 0.6em;
    border-radius: 20px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
    border: 2px solid rgba(255, 255, 255, 0.2);
}

.clock:hover {
    transform: scale(1.05);
    box-shadow: 0 12px 48px rgba(0, 0, 0, 0.4);
}

.clock:active {
    transform: scale(0.98);
}

button {
    font-size: 1.2em;
    margin: 0.5em;
    padding: 0.8em 2em;
    background: rgba(255, 255, 255, 0.2);
    backdrop-filter: blur(10px);
    border: 2px solid rgba(255, 255, 255, 0.3);
    border-radius: 50px;
    color: #fff;
    cursor: pointer;
    transition: all 0.3s ease;
    font-weight: 600;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
}

button:hover {
    background: rgba(255, 255, 255, 0.3);
    transform: translateY(-2px);
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.3);
}

button:active {
    transform: translateY(0);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
}

button:disabled {
    opacity: 0.4;
    cursor: not-allowed;
}

button:disabled:hover {
    background: rgba(255, 255, 255, 0.2);
    transform: none;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
}

.controls {
    display: flex;
    gap: 1em;
    margin-top: 2em;
    align-items: center;
}

select {
    font-size: 1.2em;
    padding: 0.8em 2em;
    background: rgba(255, 255, 255, 0.2);
    backdrop-filter: blur(10px);
    border: 2px solid rgba(255, 255, 255, 0.3);
    border-radius: 50px;
    color: #fff;
    cursor: pointer;
    transition: all 0.3s ease;
    font-weight: 600;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
    appearance: none;
    padding-right: 3em;
    background-image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12"><path fill="white" d="M6 9L1 4h10z"/></svg>');
    background-repeat: no-repeat;
    background-position: right 1em center;
}

select:hover {
    background: rgba(255, 255, 255, 0.3);
    transform: translateY(-2px);
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.3);
}

select:focus {
    outline: none;
    border-color: rgba(255, 255, 255, 0.5);
}

select option {
    background: #667eea;
    color: #fff;
    padding: 0.5em;
}

.hint {
    margin-top: 2em;
    font-size: 0.9em;
    opacity: 0.7;
    font-style: italic;
}

.status-indicator {
    position: fixed;
    top: 20px;
    right: 20px;
    display: flex;
    align-items: center;
    gap: 10px;
    background: rgba(255, 255, 255, 0.1);
    backdrop-filter: blur(10px);
    padding: 10px 20px;
    border-radius: 50px;
    border: 2px solid rgba(255, 255, 255, 0.2);
    font-size: 0.9em;
}

.status-dot {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: #888;
    transition: background 0.3s ease;
}

.status-dot.healthy {
    background: #4ade80;
    box-shadow: 0 0 10px rgba(74, 222, 128, 0.5);
}

.status-dot.pending {
    background: #fbbf24;
    box-shadow: 0 0 10px rgba(251, 191, 36, 0.5);
}

.status-dot.dead {
    background: #ef4444;
    box-shadow: 0 0 10px rgba(239, 68, 68, 0.5);
}

.status-dot.unknown {
    background: #888;
}

.modal {
    display: none;
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.5);
    backdrop-filter: blur(5px);
    align-items: center;
    justify-content: center;
    z-index: 1000;
}

.modal.active {
    display: flex;
}

.modal-content {
    background: rgba(255, 255, 255, 0.95);
    padding: 2em;
    border-radius: 20px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
    text-align: center;
    min-width: 300px;
}

.modal-content h3 {
    color: #333;
    margin-bottom: 1em;
    font-size: 1.5em;
}

.modal-content input {
    width: 100%;
    padding: 0.8em;
    font-size: 1.5em;
    border: 2px solid #667eea;
    border-radius: 10px;
    text-align: center;
    font-weight: 600;
    margin-bottom: 1em;
    color: #333;
}

.modal-content input:focus {
    outline: none;
    border-color: #764ba2;
    box-shadow: 0 0 0 3px rgba(118, 75, 162, 0.1);
}

.modal-buttons {
    display: flex;
    gap: 1em;
    justify-content: center;
}

.modal-buttons button {
    margin: 0;
    background: #667eea;
    color: #fff;
    border: none;
}

.modal-buttons button:hover {
    background: #764ba2;
}

.modal-buttons button:last-child {
    background: rgba(0, 0, 0, 0.1);
    color: #333;
}

.modal-buttons button:last-child:hover {
    background: rgba(0, 0, 0, 0.2);
}
</style>
</head>
<body>

<div class="status-indicator">
    <div class="status-dot" id="pusherStatus"></div>
    <span>Cloud Push</span>
</div>

<div class="clock" id="clock">20:00</div>

<div class="controls">
    <button onclick="toggleGame(this)">▶ Start</button>
    <select id="modeSelect" onchange="selectMode(this.value)">
        <option value="clock">🕐 Clock</option>
        <!-- Games will be populated here -->
    </select>
    <button onclick="debugEvents()">🐞 Debug Events</button>
</div>

<div class="hint">Double-click the clock to set time</div>

<div class="modal" id="timeModal">
    <div class="modal-content">
        <h3>Set Time</h3>
        <input type="text" id="timeInput" placeholder="MM:SS" />
        <div class="modal-buttons">
            <button onclick="applyTime()">Set</button>
            <button onclick="closeModal()">Cancel</button>
        </div>
    </div>
</div>

<script>
const ws = new WebSocket(`ws://${location.host}/ws`);

let currentSeconds = 1200; // Track current clock value
let currentMode = 'clock'; // Track current mode

// Fetch games and populate dropdown on page load
async function loadGames() {
    try {
        const response = await fetch('/games');
        const data = await response.json();
        const select = document.getElementById('modeSelect');

        // Clear existing game options (keep clock option)
        while (select.options.length > 1) {
            select.remove(1);
        }

        // Add game options
        data.games.forEach(game => {
            const option = document.createElement('option');
            option.value = game.game_id;
            option.textContent = `🎮 ${game.home_team} vs ${game.away_team}`;
            select.appendChild(option);
        });
    } catch (error) {
        console.error('Failed to load games:', error);
    }
}

// Load games on startup
loadGames();

ws.onmessage = (event) => {
    const data = JSON.parse(event.data).state;

    currentSeconds = data.seconds;
    currentMode = data.mode;

    // Update dropdown selection
    const modeSelect = document.getElementById('modeSelect');
    if (modeSelect.value !== data.mode) {
        modeSelect.value = data.mode;
    }

    // Update clock display based on mode
    if (data.mode === 'clock') {
        document.getElementById("clock").textContent = data.current_time;
    } else {
        const mins = Math.floor(data.seconds / 60);
        const secs = data.seconds % 60;
        document.getElementById("clock").textContent =
            `${mins}:${secs.toString().padStart(2,'0')}`;
    }

    // Update start/pause button
    const startButton = document.querySelector(".controls button:first-child");
    startButton.textContent = data.running ? "⏸ Pause" : "▶ Start";
    startButton.disabled = data.mode === 'clock';

    // Update hint text
    const hintElement = document.querySelector(".hint");
    if (data.mode === 'clock') {
        hintElement.textContent = "Showing current time";
    } else {
        if (data.current_game) {
            hintElement.textContent = `${data.current_game.home_team} vs ${data.current_game.away_team} - Double-click to set time`;
        } else {
            hintElement.textContent = "Double-click the clock to set time";
        }
    }

    // Update cloud push status indicator
    const pusherStatus = document.getElementById("pusherStatus");
    pusherStatus.className = `status-dot ${data.pusher_status}`;
};

function toggleGame(btn) {
    const running = btn.textContent.includes("Pause");
    fetch(running ? '/pause' : '/start', { method: 'POST' });
}

function selectMode(mode) {
    fetch('/select_mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: mode })
    });
}

function debugEvents() {
    fetch('/debug_events', { method: 'POST' });
}

function closeModal() {
    document.getElementById('timeModal').classList.remove('active');
}

function applyTime() {
    const newTime = document.getElementById('timeInput').value;
    if (newTime) {
        fetch('/set_time', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ time_str: newTime })
        });
    }
    closeModal();
}

document.getElementById("clock").addEventListener("dblclick", () => {
    // Only allow setting time in game mode (not clock mode)
    if (currentMode === 'clock') {
        return;
    }

    const mins = Math.floor(currentSeconds / 60);
    const secs = currentSeconds % 60;
    const currentTime = `${mins}:${secs.toString().padStart(2,'0')}`;

    document.getElementById('timeInput').value = currentTime;
    document.getElementById('timeModal').classList.add('active');
    document.getElementById('timeInput').focus();
    document.getElementById('timeInput').select();
});

// Close modal on Escape key
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeModal();
    } else if (e.key === 'Enter' && document.getElementById('timeModal').classList.contains('active')) {
        applyTime();
    }
});

// Close modal when clicking outside
document.getElementById('timeModal').addEventListener('click', (e) => {
    if (e.target.id === 'timeModal') {
        closeModal();
    }
});
</script>

</body>
</html>
"""

# ---------- Configuration ----------
from score.config import AppConfig

# ---------- SQLite setup ----------
DB_PATH = AppConfig.DB_PATH
CLOUD_API_URL = AppConfig.CLOUD_API_URL
RINK_ID = AppConfig.RINK_ID

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    logger.info("Initializing database...")
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            game_id TEXT,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            event_id INTEGER NOT NULL,
            destination TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0,
            delivered_at INTEGER,
            PRIMARY KEY (event_id, destination),
            FOREIGN KEY (event_id) REFERENCES events(id)
        )
    """)

    # Check if game_id column exists (for migration)
    cursor = db.execute("PRAGMA table_info(events)")
    columns = [col[1] for col in cursor.fetchall()]
    if "game_id" not in columns:
        logger.info("Migrating database: adding game_id column to events")
        db.execute("ALTER TABLE events ADD COLUMN game_id TEXT")

    # Add initial clock setting if this is a new database
    count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if count == 0:
        logger.info("New database - no initial events needed for clock mode")
    else:
        logger.info(f"Database initialized with {count} existing events")

    db.commit()
    db.close()

init_db()

# ---------- Game state ----------
class GameState:
    def __init__(self):
        self.seconds = 20 * 60
        self.running = False
        self.last_update = int(time.time())
        self.clients: list[WebSocket] = []
        self.pusher_status = "unknown"  # "healthy", "pending", "dead", "unknown"
        self.mode = "clock"  # "clock" or game_id
        self.current_game: Optional[dict] = None  # Current game metadata (if mode is a game_id)

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
            "mode": self.mode,
            "current_time": time.strftime("%H:%M"),
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
        logger.info(f"Fetched {len(data.get('games', []))} games from cloud API")
        return data.get("games", [])
    except Exception as e:
        logger.warning(f"Failed to fetch games from cloud API: {e}")
        return []

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

    logger.info(f"Loading state for game {game_id}...")

    result = load_game_state_from_db(DB_PATH, game_id)

    # Update global state with replayed values
    state.seconds = result["seconds"]
    state.running = result["running"]
    state.last_update = result["last_update"]

    logger.info(f"Game state loaded: {state.seconds}s, running={state.running}")
    return result["num_events"]

# ---------- Broadcast ----------
async def broadcast_state():
    data = json.dumps({"state": state.to_dict()})
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
    while True:
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
async def root():
    return html

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

@app.get("/games")
async def get_games():
    """Get available games from the cloud API."""
    games = fetch_games_from_cloud()
    return {"games": games}

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

            # If no events were found for this game, initialize with default period length
            if num_events == 0:
                state.seconds = selected_game["period_length_min"] * 60
                state.last_update = int(time.time())
                # Create CLOCK_SET event to record the initial state
                state.add_event("CLOCK_SET", {"seconds": state.seconds})
                logger.info(f"No prior state found, initializing game with {state.seconds}s")

            logger.info(f"Selected game: {selected_game['home_team']} vs {selected_game['away_team']}")
        else:
            logger.warning(f"Game {new_mode} not found in available games, switching to clock mode")
            logger.warning(f"Available game IDs were: {[g['game_id'] for g in games]}")
            state.mode = "clock"
            state.current_game = None
            state.running = False

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

    # Configure logging to send records to the queue
    queue_handler = logging.handlers.QueueHandler(log_queue)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(queue_handler)

    pusher = CloudEventPusher(
        db_path=DB_PATH,
        cloud_api_url=CLOUD_API_URL,
        device_id=AppConfig.DEVICE_ID
    )

    try:
        pusher.run()
    except KeyboardInterrupt:
        logging.info("Shutting down gracefully...")


# ---------- Run ----------
if __name__ == "__main__":
    main()

