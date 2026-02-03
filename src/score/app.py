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
from fastapi import FastAPI, WebSocket, HTTPException
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
<title>score-app | Game Clock</title>
<style>
* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
    background: #2c3e50;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    color: #ecf0f1;
}

.clock {
    font-size: 8em;
    font-weight: 700;
    margin: 0.5em;
    cursor: pointer;
    user-select: none;
    background: rgba(255, 255, 255, 0.05);
    backdrop-filter: blur(5px);
    padding: 0.3em 0.6em;
    border-radius: 12px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
    transition: transform 0.2s ease;
    border: 1px solid rgba(255, 255, 255, 0.1);
}

.clock:hover {
    transform: scale(1.02);
}

.clock:active {
    transform: scale(0.98);
}

button {
    font-size: 1.2em;
    margin: 0.5em;
    padding: 0.8em 2em;
    background: rgba(255, 255, 255, 0.08);
    backdrop-filter: blur(5px);
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 8px;
    color: #ecf0f1;
    cursor: pointer;
    transition: all 0.2s ease;
    font-weight: 500;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
}

button:hover {
    background: rgba(255, 255, 255, 0.12);
}

button:active {
    transform: scale(0.98);
}

button:disabled {
    opacity: 0.3;
    cursor: not-allowed;
}

button:disabled:hover {
    background: rgba(255, 255, 255, 0.08);
    transform: none;
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
    bottom: 20px;
    right: 20px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    background: rgba(0, 0, 0, 0.3);
    backdrop-filter: blur(5px);
    padding: 12px 16px;
    border-radius: 8px;
    border: 1px solid rgba(255, 255, 255, 0.1);
    font-size: 0.75em;
    min-width: 180px;
}

.status-item {
    display: flex;
    align-items: center;
    gap: 8px;
}

.status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #888;
    transition: background 0.3s ease;
    flex-shrink: 0;
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

/* Goal Modal Specific Styles */
.modal-content select {
    width: 100%;
    padding: 0.8em;
    font-size: 1.2em;
    border: 2px solid #667eea;
    border-radius: 10px;
    margin-bottom: 1em;
    color: #333;
    background: white;
}

.modal-content select:focus {
    outline: none;
    border-color: #764ba2;
    box-shadow: 0 0 0 3px rgba(118, 75, 162, 0.1);
}

.modal-content label {
    display: block;
    color: #333;
    font-weight: 600;
    text-align: left;
    margin-bottom: 0.5em;
    font-size: 1em;
}

.modal-content .required::after {
    content: " *";
    color: #e74c3c;
}

.modal-content .optional {
    opacity: 0.7;
}

.scoreboard {
    display: flex;
    gap: 3em;
    margin: 2em 0;
    align-items: center;
    justify-content: center;
}

.scoreboard-container {
    display: flex;
    gap: 2em;
    margin: 2em 0;
    align-items: stretch;
    justify-content: center;
    width: 100%;
    max-width: 1200px;
}

.scoreboard-container.hidden {
    display: none;
}

.scoreboard-container .clock {
    font-size: 6em;
    margin: 0;
    display: flex;
    align-items: center;
    justify-content: center;
}

.team-column {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
}

.team-header {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 1.5em;
    background: rgba(255, 255, 255, 0.05);
    backdrop-filter: blur(5px);
    border-radius: 12px;
    border: 1px solid rgba(255, 255, 255, 0.1);
    margin-bottom: 1em;
    gap: 0.5em;
    flex: 1;
    min-height: 0;
}

.team-name {
    font-size: 1.5em;
    font-weight: 600;
    opacity: 0.95;
}

.team-location {
    font-size: 0.75em;
    font-weight: 500;
    opacity: 0.5;
    text-transform: uppercase;
    letter-spacing: 0.15em;
}

.score-display {
    font-size: 4em;
    font-weight: 700;
    color: #ecf0f1;
    margin-top: 0.2em;
}

.shots-display {
    font-size: 0.9em;
    opacity: 0.6;
    font-weight: 400;
    margin-top: 0.5em;
}

.button-row {
    display: flex;
    gap: 0.5em;
    margin-bottom: 1em;
}

.add-goal-btn {
    flex: 1;
    font-size: 0.95em;
    padding: 0.7em 1.2em;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 8px;
    color: rgba(255, 255, 255, 0.85);
    cursor: pointer;
    transition: all 0.2s ease;
    font-weight: 500;
}

.add-goal-btn:hover {
    background: rgba(255, 255, 255, 0.1);
    border-color: rgba(255, 255, 255, 0.2);
}

.add-goal-btn:active {
    transform: scale(0.98);
}

.add-shot-btn {
    flex: 1;
    font-size: 0.9em;
    padding: 0.6em 1em;
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 8px;
    color: rgba(255, 255, 255, 0.7);
    cursor: pointer;
    transition: all 0.2s ease;
    font-weight: 400;
}

.add-shot-btn:hover {
    background: rgba(255, 255, 255, 0.08);
}

.add-shot-btn:active {
    transform: scale(0.98);
}

.goals-list {
    display: flex;
    flex-direction: column;
    gap: 0.4em;
    min-height: 40px;
    max-height: 300px;
    overflow-y: auto;
    padding-right: 0.3em;
}

.goals-list::-webkit-scrollbar {
    width: 6px;
}

.goals-list::-webkit-scrollbar-track {
    background: rgba(255, 255, 255, 0.05);
    border-radius: 3px;
}

.goals-list::-webkit-scrollbar-thumb {
    background: rgba(255, 255, 255, 0.2);
    border-radius: 3px;
}

.goals-list::-webkit-scrollbar-thumb:hover {
    background: rgba(255, 255, 255, 0.3);
}

.goals-list:empty::after {
    content: 'No goals yet';
    opacity: 0.4;
    font-size: 0.9em;
    font-style: italic;
    text-align: center;
    padding: 1em;
}

.goal-item {
    display: flex;
    flex-direction: column;
    gap: 0.3em;
    padding: 0.6em 0.8em;
    background: rgba(255, 255, 255, 0.05);
    border-radius: 6px;
    font-size: 0.9em;
    border: 1px solid rgba(255, 255, 255, 0.08);
    position: relative;
}

.goal-item.cancelled {
    opacity: 0.4;
    text-decoration: line-through;
}

.goal-time {
    font-family: 'Courier New', monospace;
    font-weight: 600;
    opacity: 0.9;
    font-size: 1em;
    color: #fff;
}

.goal-details {
    font-size: 0.85em;
    opacity: 0.8;
    font-weight: 400;
    display: flex;
    flex-direction: column;
    gap: 0.2em;
}

.goal-line {
    line-height: 1.4;
}

.cancel-goal-btn {
    font-size: 0.85em;
    padding: 0.3em 0.7em;
    margin: 0;
    margin-top: 0.3em;
    background: rgba(239, 68, 68, 0.15);
    border: 1px solid rgba(239, 68, 68, 0.3);
    border-radius: 4px;
    color: rgba(255, 255, 255, 0.85);
    cursor: pointer;
    transition: all 0.2s ease;
    align-self: flex-start;
}

.cancel-goal-btn:hover {
    background: rgba(239, 68, 68, 0.3);
    border-color: rgba(239, 68, 68, 0.5);
}

.cancel-goal-btn:active {
    transform: scale(0.95);
}

.cancel-goal-btn:disabled {
    opacity: 0.25;
    cursor: not-allowed;
}

.cancel-goal-btn:disabled:hover {
    transform: none;
    background: rgba(239, 68, 68, 0.15);
    border-color: rgba(239, 68, 68, 0.3);
}

.score-btn {
    font-size: 1.5em;
    padding: 0.3em 0.8em;
    margin: 0;
    background: rgba(255, 255, 255, 0.2);
    border: 2px solid rgba(255, 255, 255, 0.3);
    border-radius: 10px;
    color: #fff;
    cursor: pointer;
    transition: all 0.2s ease;
}

.score-btn:hover {
    background: rgba(255, 255, 255, 0.3);
    transform: scale(1.1);
}

.score-btn:active {
    transform: scale(0.95);
}

.scoreboard.hidden {
    display: none;
}
</style>
</head>
<body>

<div class="status-indicator">
    <div class="status-item">
        <div class="status-dot" id="assignmentStatus"></div>
        <span>Device Assignment</span>
    </div>
    <div class="status-item">
        <div class="status-dot" id="scheduleStatus"></div>
        <span>Day Schedule</span>
    </div>
    <div class="status-item">
        <div class="status-dot" id="pusherStatus"></div>
        <span>Cloud Push</span>
    </div>
</div>

<div class="scoreboard-container hidden" id="scoreboardContainer">
    <div class="team-column">
        <div class="team-header">
            <div class="team-name" id="homeTeam">Home</div>
            <div class="team-location">HOME</div>
            <div class="score-display" id="homeScore">0</div>
            <div class="shots-display">Shots: <span id="homeShots">0</span></div>
        </div>
        <div class="button-row">
            <button class="add-goal-btn" onclick="addGoal('home')">+ Goal</button>
            <button class="add-shot-btn" onclick="addShot('home')">+ Shot</button>
        </div>
        <div class="goals-list" id="homeGoals"></div>
    </div>

    <div class="clock" id="clock">20:00</div>

    <div class="team-column">
        <div class="team-header">
            <div class="team-name" id="awayTeam">Away</div>
            <div class="team-location">AWAY</div>
            <div class="score-display" id="awayScore">0</div>
            <div class="shots-display">Shots: <span id="awayShots">0</span></div>
        </div>
        <div class="button-row">
            <button class="add-goal-btn" onclick="addGoal('away')">+ Goal</button>
            <button class="add-shot-btn" onclick="addShot('away')">+ Shot</button>
        </div>
        <div class="goals-list" id="awayGoals"></div>
    </div>
</div>

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

<div class="modal" id="goalModal">
    <div class="modal-content">
        <h3>Record Goal</h3>
        <input type="hidden" id="goalTeam" />

        <label class="required" for="goalScorer">Scorer</label>
        <select id="goalScorer">
            <option value="">-- Select Scorer --</option>
        </select>

        <label class="required" for="goalAssist1">Primary Assist</label>
        <select id="goalAssist1">
            <option value="">-- Select Assist (or None) --</option>
            <option value="none">Unassisted</option>
        </select>

        <label class="optional" for="goalAssist2">Secondary Assist</label>
        <select id="goalAssist2">
            <option value="">-- None --</option>
        </select>

        <div class="modal-buttons">
            <button onclick="submitGoal()">Record Goal</button>
            <button onclick="closeGoalModal()">Cancel</button>
        </div>
    </div>
</div>

<script>
const ws = new WebSocket(`ws://${location.host}/ws`);

let currentSeconds = 1200; // Track current clock value
let currentMode = 'clock'; // Track current mode
let wasAssigned = false; // Track if device was previously assigned


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

    // Cache state for modal access
    let cacheEl = document.getElementById('stateCache');
    if (!cacheEl) {
        cacheEl = document.createElement('script');
        cacheEl.id = 'stateCache';
        cacheEl.type = 'application/json';
        document.body.appendChild(cacheEl);
    }
    cacheEl.textContent = JSON.stringify(data);

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

    // Update scoreboard visibility and content
    const scoreboardContainer = document.getElementById("scoreboardContainer");
    if (data.mode === 'clock') {
        scoreboardContainer.classList.add('hidden');
    } else {
        scoreboardContainer.classList.remove('hidden');

        // Update team names
        if (data.current_game) {
            document.getElementById("homeTeam").textContent = data.current_game.home_team;
            document.getElementById("awayTeam").textContent = data.current_game.away_team;
        }

        // Update scores
        document.getElementById("homeScore").textContent = data.home_score;
        document.getElementById("awayScore").textContent = data.away_score;

        // Update shots
        document.getElementById("homeShots").textContent = data.home_shots || 0;
        document.getElementById("awayShots").textContent = data.away_shots || 0;

        // Update goals lists (separate for each team)
        renderGoalsList(data.goals, data.roster_details);
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

    // Update assignment status indicator
    const assignmentStatus = document.getElementById("assignmentStatus");
    assignmentStatus.className = `status-dot ${data.assignment_status}`;

    // Update schedule status indicator
    const scheduleStatus = document.getElementById("scheduleStatus");
    scheduleStatus.className = `status-dot ${data.schedule_status}`;

    // If device just got assigned, reload games
    if (data.device_assigned && !wasAssigned) {
        console.log('Device just got assigned - loading games...');
        loadGames();
        wasAssigned = true;
    } else if (!data.device_assigned) {
        wasAssigned = false;
    }
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

function addGoal(team) {
    // Check if rosters are loaded
    const state = JSON.parse(document.getElementById('stateCache')?.textContent || '{}');

    if (!state.roster_loaded) {
        // No roster loaded - submit anonymous goal immediately
        submitAnonymousGoal(team);
        return;
    }

    // Open modal for player selection
    openGoalModal(team);
}

function submitAnonymousGoal(team) {
    // Submit goal without player information
    fetch('/add_goal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            team: team,
            scorer_id: null,
            assist1_id: null,
            assist2_id: null
        })
    });
}

function openGoalModal(team) {
    const state = JSON.parse(document.getElementById('stateCache')?.textContent || '{}');
    const modal = document.getElementById('goalModal');
    document.getElementById('goalTeam').value = team;

    // Populate dropdowns with roster
    const roster = team === 'home' ? state.home_roster : state.away_roster;
    const rosterDetails = state.roster_details;

    const scorerSelect = document.getElementById('goalScorer');
    const assist1Select = document.getElementById('goalAssist1');
    const assist2Select = document.getElementById('goalAssist2');

    // Clear existing options (keep first/default option)
    scorerSelect.options.length = 1;
    assist1Select.options.length = 2; // Keep default + "Unassisted"
    assist2Select.options.length = 1;

    // Populate with players (sorted by jersey number)
    const sortedRoster = roster
        .map(id => rosterDetails[id])
        .filter(p => p) // Remove null/undefined
        .sort((a, b) => (a.jersey_number || 999) - (b.jersey_number || 999));

    sortedRoster.forEach(player => {
        const optionText = `#${player.jersey_number || '?'} ${player.full_name}`;

        scorerSelect.add(new Option(optionText, player.player_id));
        assist1Select.add(new Option(optionText, player.player_id));
        assist2Select.add(new Option(optionText, player.player_id));
    });

    // Reset selections
    scorerSelect.value = '';
    assist1Select.value = '';
    assist2Select.value = '';

    modal.classList.add('active');
    scorerSelect.focus();
}

function closeGoalModal() {
    document.getElementById('goalModal').classList.remove('active');
}

function submitGoal() {
    const team = document.getElementById('goalTeam').value;
    const scorer = document.getElementById('goalScorer').value;
    const assist1 = document.getElementById('goalAssist1').value;
    const assist2 = document.getElementById('goalAssist2').value;

    // Validate required fields
    if (!scorer) {
        alert('Please select a scorer');
        return;
    }

    if (!assist1) {
        alert('Please select primary assist or "Unassisted"');
        return;
    }

    // Submit goal with player information
    fetch('/add_goal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            team: team,
            scorer_id: scorer,
            assist1_id: assist1 === 'none' ? null : assist1,
            assist2_id: assist2 || null
        })
    });

    closeGoalModal();
}

function cancelGoal(goalId) {
    fetch('/cancel_goal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal_id: goalId })
    });
}

function addShot(team) {
    fetch('/add_shot', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ team: team })
    });
}

function renderGoalsList(goals, rosterDetails) {
    const homeContainer = document.getElementById('homeGoals');
    const awayContainer = document.getElementById('awayGoals');

    if (!goals || goals.length === 0) {
        homeContainer.innerHTML = '';
        awayContainer.innerHTML = '';
        return;
    }

    // Helper to format player display
    function formatPlayer(playerId) {
        if (!playerId || !rosterDetails) return '';
        const player = rosterDetails[playerId];
        if (!player) return 'Unknown';
        return `#${player.jersey_number || '?'} ${player.full_name}`;
    }

    function formatGoalDetails(goal) {
        if (!goal.scorer_id) {
            return '<div class="goal-details"><div class="goal-line">Unknown scorer</div></div>';
        }

        const scorer = formatPlayer(goal.scorer_id);
        let detailsHtml = '<div class="goal-details">';

        // Goal line
        detailsHtml += `<div class="goal-line">Goal: ${scorer}</div>`;

        // Assist 1 line
        if (goal.assist1_id) {
            detailsHtml += `<div class="goal-line">Assist 1: ${formatPlayer(goal.assist1_id)}</div>`;
        } else {
            detailsHtml += '<div class="goal-line">Unassisted</div>';
        }

        // Assist 2 line (only if present)
        if (goal.assist2_id) {
            detailsHtml += `<div class="goal-line">Assist 2: ${formatPlayer(goal.assist2_id)}</div>`;
        }

        detailsHtml += '</div>';
        return detailsHtml;
    }

    // Split goals by team and render newest first
    const homeGoals = goals.filter(g => g.team === 'home').reverse();
    const awayGoals = goals.filter(g => g.team === 'away').reverse();

    homeContainer.innerHTML = homeGoals.map(goal => {
        const cancelledClass = goal.cancelled ? 'cancelled' : '';
        const disabledAttr = goal.cancelled ? 'disabled' : '';

        return `
            <div class="goal-item ${cancelledClass}">
                <span class="goal-time">${goal.time}</span>
                ${formatGoalDetails(goal)}
                <button class="cancel-goal-btn" onclick="cancelGoal('${goal.id}')" ${disabledAttr}>
                    ${goal.cancelled ? 'Cancelled' : 'Cancel'}
                </button>
            </div>
        `;
    }).join('');

    awayContainer.innerHTML = awayGoals.map(goal => {
        const cancelledClass = goal.cancelled ? 'cancelled' : '';
        const disabledAttr = goal.cancelled ? 'disabled' : '';

        return `
            <div class="goal-item ${cancelledClass}">
                <span class="goal-time">${goal.time}</span>
                ${formatGoalDetails(goal)}
                <button class="cancel-goal-btn" onclick="cancelGoal('${goal.id}')" ${disabledAttr}>
                    ${goal.cancelled ? 'Cancelled' : 'Cancel'}
                </button>
            </div>
        `;
    }).join('');
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
        closeGoalModal();
    } else if (e.key === 'Enter' && document.getElementById('timeModal').classList.contains('active')) {
        applyTime();
    } else if (e.key === 'Enter' && document.getElementById('goalModal').classList.contains('active')) {
        submitGoal();
    }
});

// Close modal when clicking outside
document.getElementById('timeModal').addEventListener('click', (e) => {
    if (e.target.id === 'timeModal') {
        closeModal();
    }
});

// Space bar to toggle running/paused
document.addEventListener('keydown', (e) => {
    if (e.key === ' ' || e.code === 'Space') {
        // Only toggle if we're in game mode (not clock mode)
        if (currentMode !== 'clock') {
            e.preventDefault(); // Prevent page scroll
            const startButton = document.querySelector(".controls button:first-child");
            toggleGame(startButton);
        }
    }
});
</script>

</body>
</html>
"""

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

    logger.info(f"Loading state for game {game_id}...")

    result = load_game_state_from_db(DB_PATH, game_id)

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

