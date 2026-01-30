from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from typing import Optional, List
import asyncio
import datetime

app = FastAPI()
templates = Jinja2Templates(directory="templates")

def parse_time_to_seconds(time_str: str) -> int:
    try:
        if ":" in time_str:
            minutes, seconds = map(int, time_str.split(":"))
            return minutes * 60 + seconds
        else:
            return int(time_str)
    except ValueError:
        return 0 # Should ideally raise an HTTPException

def format_seconds_to_time_str(seconds: int) -> str:
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes:02d}:{secs:02d}"

class GameEngine:
    def __init__(self):
        self.period = 1
        self.time = 1200  # 20 minutes in seconds
        self.clock_running = False
        self.home_score = 0
        self.away_score = 0
        self.penalties = []  # list of dicts
        self.event_log = []
        self.clock_task = None
        self.next_penalty_id = 1
        self.game_over = False

    async def _clock_tick(self):
        while self.time > 0 and self.clock_running:
            await asyncio.sleep(1)
            self.time -= 1
            for penalty in self.penalties:
                penalty["remaining"] -= 1
            self.penalties = [p for p in self.penalties if p["remaining"] > 0]
        
        if self.time == 0:
            self.stop_clock()
            if self.period < 3:
                self.period += 1
                self.time = 1200
            else:
                self.game_over = True
        
        if not self.game_over:
            self.clock_running = False

    def start_clock(self):
        if not self.clock_running and not self.game_over:
            self.clock_running = True
            self.clock_task = asyncio.create_task(self._clock_tick())

    def stop_clock(self):
        self.clock_running = False
        if self.clock_task:
            self.clock_task.cancel()

    def _format_event_message(self, event):
        game_time_str = format_seconds_to_time_str(event["game_time"])
        if event["type"] == "goal":
            goal_info = f"GOAL for {event['team'].upper()} by {event['player']}"
            if event['assists']:
                goal_info += f" (Assists: {', '.join(event['assists'])})"
            return f"Period {event['period']} {game_time_str} - {goal_info}!"
        elif event["type"] == "penalty":
            duration_formatted = format_seconds_to_time_str(event["duration"])
            return f"Period {event['period']} {game_time_str} - PENALTY for {event['player']} ({event['team']}) - {duration_formatted}"
        return f"Period {event['period']} {game_time_str} - {event['type']} event"


    def goal_scored(self, team: str, player: str, assists: Optional[List[str]] = None):
        if team == "home":
            self.home_score += 1
        else:
            self.away_score += 1
        self.event_log.append({"type": "goal", "team": team, "player": player, "assists": assists or [], "game_time": self.time, "period": self.period})

        # Infer power play: if opposing team has penalties, clear the oldest one
        if team == "home":
            opposing_team = "away"
        else:
            opposing_team = "home"
        
        # Sort by oldest penalty first (lower ID means older)
        opposing_penalties = sorted([p for p in self.penalties if p.get("team") == opposing_team], key=lambda x: x['id'])
        
        if opposing_penalties:
            self.remove_penalty(opposing_penalties[0]['id'])


    def add_penalty(self, player, duration_seconds, team):
        penalty_id = self.next_penalty_id
        self.penalties.append({"id": penalty_id, "player": player, "remaining": duration_seconds, "team": team})
        self.event_log.append(
            {"type": "penalty", "id": penalty_id, "player": player, "duration": duration_seconds, "team": team, "game_time": self.time, "period": self.period}
        )
        self.next_penalty_id += 1
        
    def remove_penalty(self, penalty_id: int):
        self.penalties = [p for p in self.penalties if p.get("id") != penalty_id]

    def reset(self):
        self.stop_clock()
        self.period = 1
        self.time = 1200
        self.home_score = 0
        self.away_score = 0
        self.penalties = []
        self.event_log = []
        self.next_penalty_id = 1
        self.game_over = False

engine = GameEngine()

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "engine": engine})

@app.post("/start")
async def start():
    engine.start_clock()
    return {"status": "clock started"}

@app.post("/stop")
async def stop():
    engine.stop_clock()
    return {"status": "clock stopped"}

@app.post("/goal")
async def goal(
    team: str = Form(...),
    player: str = Form(...),
    assist1: Optional[str] = Form(None),
    assist2: Optional[str] = Form(None)
):
    assists = [a for a in [assist1, assist2] if a]
    engine.goal_scored(team, player, assists)
    return {"status": f"goal for {team} by {player}"}

@app.post("/penalty")
async def penalty(player: str = Form(...), duration: str = Form(...), team: str = Form(...)):
    duration_seconds = parse_time_to_seconds(duration)
    engine.add_penalty(player, duration_seconds, team)
    return {"status": f"penalty for {player} on {team}"}
    
@app.delete("/penalty/{penalty_id}")
async def delete_penalty(penalty_id: int):
    engine.remove_penalty(penalty_id)
    return {"status": f"penalty {penalty_id} removed"}

@app.get("/time")
async def time():
    return {"time": engine.time, "running": engine.clock_running}
    
@app.get("/game_status")
async def game_status():
    return {"period": engine.period, "game_over": engine.game_over}

@app.get("/home_penalties")
async def home_penalties():
    formatted_penalties = [
        {**p, "remaining_formatted": format_seconds_to_time_str(p["remaining"])}
        for p in engine.penalties if p["team"] == "home"
    ]
    return {"penalties": formatted_penalties}

@app.get("/away_penalties")
async def away_penalties():
    formatted_penalties = [
        {**p, "remaining_formatted": format_seconds_to_time_str(p["remaining"])}
        for p in engine.penalties if p["team"] == "away"
    ]
    return {"penalties": formatted_penalties}

@app.get("/scores")
async def scores():
    return {"home_score": engine.home_score, "away_score": engine.away_score}

@app.get("/home_goal_log")
async def home_goal_log():
    formatted_home_goal_log = [engine._format_event_message(event) for event in engine.event_log if event["type"] == "goal" and event["team"] == "home"]
    return {"home_goal_log": formatted_home_goal_log}

@app.get("/away_goal_log")
async def away_goal_log():
    formatted_away_goal_log = [engine._format_event_message(event) for event in engine.event_log if event["type"] == "goal" and event["team"] == "away"]
    return {"away_goal_log": formatted_away_goal_log}

@app.get("/home_penalty_log")
async def home_penalty_log():
    formatted_home_penalty_log = [engine._format_event_message(event) for event in engine.event_log if event["type"] == "penalty" and event["team"] == "home"]
    return {"home_penalty_log": formatted_home_penalty_log}

@app.get("/away_penalty_log")
async def away_penalty_log():
    formatted_away_penalty_log = [engine._format_event_message(event) for event in engine.event_log if event["type"] == "penalty" and event["team"] == "away"]
    return {"away_penalty_log": formatted_away_penalty_log}

@app.post("/reset")
async def reset():
    engine.reset()
    return {"status": "game reset"}