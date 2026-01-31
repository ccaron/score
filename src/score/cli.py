import asyncio
import json
import time
import sqlite3
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import uvicorn
import webview

# ---------- Inline HTML + JS ----------
html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Game Clock</title>
<style>
body { font-family: sans-serif; text-align: center; background: #f5f5f5; }
.clock { font-size: 5em; margin: 1em; cursor: pointer; user-select: none; }
button { font-size: 1.2em; margin: 0.5em; padding: 0.5em 1em; }
.stats { font-size: 1.5em; margin: 1em; }
</style>
</head>
<body>

<div class="clock" id="clock">20:00</div>

<div class="stats">
    <div>Home: <span id="home_score">0</span></div>
    <div>Away: <span id="away_score">0</span></div>
    <div>Period: <span id="period">1</span></div>
</div>

<div>
    <button onclick="toggleGame(this)">▶ Start</button>
    <button onclick="debugEvents()">🐞 Debug Events</button>
</div>

<script>
const ws = new WebSocket(`ws://${location.host}/ws`);

ws.onmessage = (event) => {
    const data = JSON.parse(event.data).state;

    const mins = Math.floor(data.seconds / 60);
    const secs = data.seconds % 60;
    document.getElementById("clock").textContent =
        `${mins}:${secs.toString().padStart(2,'0')}`;

    document.getElementById("home_score").textContent = data.home_score;
    document.getElementById("away_score").textContent = data.away_score;
    document.getElementById("period").textContent = data.period;

    document.querySelector("button").textContent =
        data.running ? "⏸ Pause" : "▶ Start";
};

function toggleGame(btn) {
    const running = btn.textContent.includes("Pause");
    fetch(running ? '/pause' : '/start', { method: 'POST' });
}

function debugEvents() {
    fetch('/debug_events', { method: 'POST' });
}
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
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)

    # Add initial clock setting if this is a new database
    count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if count == 0:
        db.execute(
            "INSERT INTO events (type, payload, created_at) VALUES (?, ?, ?)",
            ("CLOCK_SET", json.dumps({"seconds": 20 * 60}), int(time.time()))
        )

    db.commit()
    db.close()

init_db()

# ---------- Game state ----------
class GameState:
    def __init__(self):
        self.seconds = 20 * 60
        self.running = False
        self.home_score = 0
        self.away_score = 0
        self.period = 1
        self.last_update = int(time.time())
        self.clients: list[WebSocket] = []

    def add_event(self, event_type, payload=None):
        db = get_db()
        db.execute(
            "INSERT INTO events (type, payload, created_at) VALUES (?, ?, ?)",
            (event_type, json.dumps(payload or {}), int(time.time()))
        )
        db.commit()
        db.close()

    def to_dict(self):
        return {
            "seconds": self.seconds,
            "running": self.running,
            "home_score": self.home_score,
            "away_score": self.away_score,
            "period": self.period,
        }

state = GameState()

# ---------- State replay ----------
def load_state_from_events():
    db = get_db()
    rows = db.execute(
        "SELECT type, payload, created_at FROM events ORDER BY created_at ASC"
    ).fetchall()
    db.close()

    for r in rows:
        payload = json.loads(r["payload"])
        if r["type"] == "CLOCK_SET":
            state.seconds = payload["seconds"]
        elif r["type"] == "GAME_STARTED":
            state.running = True
            state.last_update = r["created_at"]
        elif r["type"] == "GAME_PAUSED":
            # Calculate how much time elapsed while running
            if state.running:
                elapsed = r["created_at"] - state.last_update
                state.seconds = max(0, state.seconds - elapsed)
            state.running = False
            state.last_update = r["created_at"]

    # Correct for elapsed wall time if still running
    if state.running:
        elapsed = int(time.time()) - state.last_update
        state.seconds = max(0, state.seconds - elapsed)

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

# ---------- Game loop ----------
async def game_loop():
    while True:
        if state.running and state.seconds > 0:
            state.seconds -= 1
            state.last_update = int(time.time())
            await broadcast_state()
        await asyncio.sleep(1)

# ---------- Lifespan ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state_from_events()
    asyncio.create_task(game_loop())
    yield

app = FastAPI(lifespan=lifespan)

# ---------- Routes ----------
@app.get("/", response_class=HTMLResponse)
async def root():
    return html

@app.post("/start")
async def start_game():
    if not state.running:
        state.running = True
        state.last_update = int(time.time())
        state.add_event("GAME_STARTED")
        await broadcast_state()
    return {"status": "ok"}

@app.post("/pause")
async def pause_game():
    if state.running:
        state.running = False
        state.add_event("GAME_PAUSED")
        await broadcast_state()
    return {"status": "ok"}

@app.post("/set_time")
async def set_time(time_str: str):
    mins, secs = map(int, time_str.split(":"))
    state.seconds = mins * 60 + secs
    state.last_update = int(time.time())
    state.add_event("CLOCK_SET", {"seconds": state.seconds})
    await broadcast_state()
    return {"status": "ok"}

@app.post("/debug_events")
async def debug_events():
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

    await ws.send_text(json.dumps({"state": state.to_dict()}))

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        state.clients.remove(ws)

def main():
    def run_server():
        uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")

    threading.Thread(target=run_server, daemon=True).start()
    time.sleep(0.5)

    webview.create_window("Game Clock", "http://127.0.0.1:8000")
    webview.start()
    

# ---------- Run ----------
if __name__ == "__main__":
    main()

