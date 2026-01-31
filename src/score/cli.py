import asyncio
import json
import logging
import logging.handlers
import multiprocessing
import time
import sqlite3
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from rich.console import ConsoleRenderable
from rich.logging import RichHandler
import uvicorn
import webview

# Set up logger for this module
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
        format="[dim cyan][PID: %(process)d TID: %(thread)d][/dim cyan] %(message)s",
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

.controls {
    display: flex;
    gap: 1em;
    margin-top: 2em;
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
    <span>Event Pusher</span>
</div>

<div class="clock" id="clock">20:00</div>

<div class="controls">
    <button onclick="toggleGame(this)">▶ Start</button>
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

ws.onmessage = (event) => {
    const data = JSON.parse(event.data).state;

    currentSeconds = data.seconds;
    const mins = Math.floor(data.seconds / 60);
    const secs = data.seconds % 60;
    document.getElementById("clock").textContent =
        `${mins}:${secs.toString().padStart(2,'0')}`;

    document.querySelector("button").textContent =
        data.running ? "⏸ Pause" : "▶ Start";

    // Update pusher status indicator
    const pusherStatus = document.getElementById("pusherStatus");
    pusherStatus.className = `status-dot ${data.pusher_status}`;
};

function toggleGame(btn) {
    const running = btn.textContent.includes("Pause");
    fetch(running ? '/pause' : '/start', { method: 'POST' });
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

# ---------- SQLite setup ----------
DB_PATH = "game.db"

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

    # Add initial clock setting if this is a new database
    count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if count == 0:
        logger.info("New database - adding initial CLOCK_SET event")
        db.execute(
            "INSERT INTO events (type, payload, created_at) VALUES (?, ?, ?)",
            ("CLOCK_SET", json.dumps({"seconds": 20 * 60}), int(time.time()))
        )
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

    def add_event(self, event_type, payload=None):
        logger.debug(f"Adding event: {event_type} with payload: {payload}")
        db = get_db()
        db.execute(
            "INSERT INTO events (type, payload, created_at) VALUES (?, ?, ?)",
            (event_type, json.dumps(payload or {}), int(time.time()))
        )
        db.commit()
        db.close()

    def has_undelivered_events(self, destination="events.log"):
        """Check if there are any undelivered events for the given destination."""
        db = get_db()
        count = db.execute("""
            SELECT COUNT(*) FROM events e
            LEFT JOIN deliveries d ON e.id = d.event_id AND d.destination = ?
            WHERE d.event_id IS NULL OR d.delivered IN (0, 2)
        """, (destination,)).fetchone()[0]
        db.close()
        return count > 0

    def to_dict(self):
        return {
            "seconds": self.seconds,
            "running": self.running,
            "pusher_status": self.pusher_status,
        }

state = GameState()

# Global reference to pusher process for health checks
pusher_process = None

# ---------- State replay ----------
def load_state_from_events():
    logger.info("Loading state from events...")
    db = get_db()
    rows = db.execute(
        "SELECT type, payload, created_at FROM events ORDER BY created_at ASC"
    ).fetchall()
    db.close()

    logger.info(f"Replaying {len(rows)} events")
    for r in rows:
        payload = json.loads(r["payload"])
        if r["type"] == "CLOCK_SET":
            state.seconds = payload["seconds"]
            logger.debug(f"Replayed CLOCK_SET: {state.seconds}s")
        elif r["type"] == "GAME_STARTED":
            state.running = True
            state.last_update = r["created_at"]
            logger.debug("Replayed GAME_STARTED")
        elif r["type"] == "GAME_PAUSED":
            # Calculate how much time elapsed while running
            if state.running:
                elapsed = r["created_at"] - state.last_update
                state.seconds = max(0, state.seconds - elapsed)
            state.running = False
            state.last_update = r["created_at"]
            logger.debug(f"Replayed GAME_PAUSED: {state.seconds}s remaining")

    # Correct for elapsed wall time if still running
    if state.running:
        elapsed = int(time.time()) - state.last_update
        state.seconds = max(0, state.seconds - elapsed)
        logger.info(f"Game is running - adjusted for {elapsed}s elapsed time")

    logger.info(f"State loaded: {state.seconds}s, running={state.running}")

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
        # Check pusher health and delivery status
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
            # Even if not running, broadcast occasionally to update pusher status
            await broadcast_state()

        await asyncio.sleep(1)

# ---------- Lifespan ----------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Starting application...")
    load_state_from_events()
    asyncio.create_task(game_loop())
    logger.info("Application started")
    yield
    logger.info("Application shutting down")

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
        print(
            f"{r['id']:03d} | {r['type']:<15} | "
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
        state.clients.remove(ws)
        logger.info(f"WebSocket client disconnected (total: {len(state.clients)})")

def main():
    global pusher_process

    # Configure logging first - this will handle all log records
    init_logging()

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

    # Start event pusher in a separate process
    pusher_process = multiprocessing.Process(
        target=push_events,
        args=(log_queue,),
        name="EventPusher"
    )
    pusher_process.start()
    logger.info(f"Event pusher process started (PID: {pusher_process.pid})")

    def run_server():
        uvicorn.run(app, host="127.0.0.1", port=8000, log_config=None)

    threading.Thread(target=run_server, daemon=True).start()
    time.sleep(0.5)

    logger.info("Opening webview window")
    try:
        webview.create_window("Game Clock", "http://127.0.0.1:8000")
        webview.start()
    finally:
        logger.info("Main window closed, terminating event pusher")
        pusher_process.terminate()
        pusher_process.join(timeout=5)
        queue_listener.stop()
        logger.info("Shutdown complete")


def push_events(log_queue):
    """
    Start the event pusher worker process.

    Args:
        log_queue: multiprocessing.Queue for sending log records to main process
    """
    from score.event_pusher import FileEventPusher

    # Configure logging to send records to the queue
    queue_handler = logging.handlers.QueueHandler(log_queue)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(queue_handler)

    pusher = FileEventPusher(
        db_path=DB_PATH,
        output_path="events.log"
    )

    try:
        pusher.run()
    except KeyboardInterrupt:
        logging.info("Shutting down gracefully...")


# ---------- Run ----------
if __name__ == "__main__":
    main()

