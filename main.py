from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory="templates")

class GameEngine:
    def __init__(self):
        self.period = 1
        self.clock_running = False
        self.home_score = 0
        self.away_score = 0
        self.penalties = []  # list of dicts
        self.event_log = []

    def start_clock(self):
        self.clock_running = True

    def stop_clock(self):
        self.clock_running = False

    def goal_scored(self, team):
        if team == "home":
            self.home_score += 1
        else:
            self.away_score += 1
        self.event_log.append({"type": "goal", "team": team})

    def add_penalty(self, player, duration):
        self.penalties.append({"player": player, "remaining": duration})
        self.event_log.append(
            {"type": "penalty", "player": player, "duration": duration}
        )

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
async def goal(team: str = Form(...)):
    engine.goal_scored(team)
    return {"status": f"goal for {team}"}

@app.post("/penalty")
async def penalty(player: str = Form(...), duration: int = Form(...)):
    engine.add_penalty(player, duration)
    return {"status": f"penalty for {player}"}
