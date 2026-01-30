import threading
import time
import json
import os
import sys
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import webview
import sqlite3

# ---------- FastAPI setup ----------
app = FastAPI()

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
    document.getElementById("clock").textContent = `${mins}:${secs.toString().padStart(2,'0')}`;

    document.getElementById("home_score").textContent = data.home_score;
    document.getElementById("away_score").textContent = data.away_score;
    document.getElementById("period").textContent = data.period;

    document.querySelector("button").textContent = data.running ? "⏸ Pause" : "▶ Start";
};

// Toggle start/pause
function toggleGame(btn) {
    const running = btn.textContent.includes("Pause");
    fetch(running ? '/pause' : '/start', {method:'POST'});
}

// Double-click clock to set time
document.getElementById("clock").addEventListener("dblclick", async () => {
    const current = document.getElementById("clock").textContent;
    const timeStr = prompt("Enter time (MM:SS):", current);
    if (timeStr) await fetch(`/set_time?time_str=${encodeURIComponent(timeStr)}`, {method:'POST'});
});

// Debug button
function debugEvents() { fetch('/debug_events', {method:'POST'}); }
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

# Initialize DB
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
    db.commit()
    db.close()

init_db()

# ---------- Game state ----------
class GameState:
    def __init__(self):
        self.seconds = 20*60
        self.running = False
        self.home_score = 0
        self.away_score = 0
        self.period = 1
        self.clients = []

    def add_event(self, event_type, payload=None):
        db = get_db()
        db.execute(
            "INSERT INTO events (type, payload, created_at) VALUES (?, ?, ?)",
            (event_type, json.dumps(payload or {}), int(time.time()))
        )
        db.commit()
        db.close()

state = GameState()

# ---------- Routes ----------
@app.get("/", response_class=HTMLResponse)
def root():
    return html

@app.post("/start")
def start_game():
    if not state.running:
        state.running = True
        state.add_event("GAME_STARTED")
    return {"status":"ok"}

@app.post("/pause")
def pause_game():
    if state.running:
        state.running = False
        state.add_event("GAME_PAUSED")
    return {"status":"ok"}

@app.post("/set_time")
def set_time(time_str: str):
    try:
        mins, secs = map(int, time_str.split(":"))
        state.seconds = mins*60 + secs
        state.add_event("CLOCK_SET", {"seconds": state.seconds})
        broadcast_state()
    except:
        return {"status":"error","msg":"Invalid format"}
    return {"status":"ok"}

@app.post("/debug_events")
def debug_events():
    db = get_db()
    rows = db.execute("SELECT * FROM events ORDER BY created_at ASC").fetchall()
    db.close()
    print("\n===== DEBUG EVENTS =====")
    print(f"{'ID':>3} | {'TYPE':<15} | {'PAYLOAD':<40} | {'TIME'}")
    print("-"*80)
    for r in rows:
        print(f"{r['id']:03d} | {r['type']:<15} | {r['payload']:<40} | {time.ctime(r['created_at'])}")
    print("="*80+"\n")
    return {"status":"events printed"}

# ---------- WebSocket ----------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.clients.append(ws)
    try:
        await ws.send_text(json.dumps({"state": vars(state)}))
        while True:
            await ws.receive_text()
    except:
        pass
    finally:
        state.clients.remove(ws)

def broadcast_state():
    data = json.dumps({"state": vars(state)})
    for ws in list(state.clients):
        try:
            import asyncio
            asyncio.create_task(ws.send_text(data))
        except:
            state.clients.remove(ws)

# ---------- Game loop ----------
def game_loop():
    while True:
        if state.running and state.seconds > 0:
            state.seconds -= 1
            broadcast_state()
        time.sleep(1)

# ---------- Run server + GUI ----------
def start_server():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")

if __name__ == "__main__":
    threading.Thread(target=game_loop, daemon=True).start()
    threading.Thread(target=start_server, daemon=True).start()
    webview.create_window("Game Clock", "http://127.0.0.1:8000")
    webview.start()

