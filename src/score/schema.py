"""
Cloud database schema for Score.

This module defines all table schemas for the cloud database (cloud.db).
Tables are created in dependency order to respect foreign key constraints.
"""

import sqlite3
import time
import logging

logger = logging.getLogger("score.schema")

SCHEMA_VERSION = "2.0.0"

# =============================================================================
# Table Definitions (in dependency order)
# =============================================================================

TABLES = """
-- =============================================================================
-- PERMANENT ENTITIES (no dependencies)
-- =============================================================================

-- Organizations that run competitions
CREATE TABLE IF NOT EXISTS leagues (
    league_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    league_type TEXT,                -- "professional", "amateur", "rec"
    description TEXT,
    website TEXT,
    logo_url TEXT,
    created_at INTEGER NOT NULL
);

-- Time periods (independent of leagues)
CREATE TABLE IF NOT EXISTS seasons (
    season_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,              -- "2025-2026", "Winter 2026"
    start_date TEXT NOT NULL,        -- ISO 8601
    end_date TEXT,
    created_at INTEGER NOT NULL
);

-- Team groupings (reusable across seasons)
CREATE TABLE IF NOT EXISTS divisions (
    division_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    division_type TEXT,              -- "conference", "division", "bracket", "pool"
    parent_division_id TEXT,         -- For conference→division nesting
    description TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (parent_division_id) REFERENCES divisions(division_id)
);

-- Time-bound events (alternative to league+season)
CREATE TABLE IF NOT EXISTS tournaments (
    tournament_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    location TEXT,
    tournament_type TEXT,            -- "championship", "invitational", "playoff"
    description TEXT,
    created_at INTEGER NOT NULL
);

-- Team organizations
CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT,
    abbreviation TEXT,               -- "TOR", "MTL"
    team_type TEXT,                  -- "franchise", "club", "pickup"
    logo_url TEXT,
    primary_color TEXT,
    secondary_color TEXT,
    created_at INTEGER NOT NULL
);

-- Individual athletes
CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    birth_date TEXT,
    birth_city TEXT,
    birth_country TEXT,
    height_inches INTEGER,
    weight_pounds INTEGER,
    shoots_catches TEXT,             -- "L", "R"
    public_email TEXT,               -- Optional, for spare contact
    public_phone TEXT,               -- Optional, for spare contact
    created_at INTEGER NOT NULL
);

-- Physical venues
CREATE TABLE IF NOT EXISTS rinks (
    rink_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT,
    city TEXT,
    province_state TEXT,
    postal_code TEXT,
    country TEXT,
    phone TEXT,
    website TEXT,
    parking_info TEXT,
    notes TEXT,
    created_at INTEGER NOT NULL
);

-- Referees and linesmen
CREATE TABLE IF NOT EXISTS officials (
    official_id TEXT PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    certification_level TEXT,
    created_at INTEGER NOT NULL
);

-- =============================================================================
-- DEPENDENT PERMANENT ENTITIES
-- =============================================================================

-- Ice surfaces within a rink
CREATE TABLE IF NOT EXISTS rink_sheets (
    sheet_id TEXT PRIMARY KEY,
    rink_id TEXT NOT NULL,
    name TEXT NOT NULL,              -- "Sheet A", "Main Rink"
    surface_type TEXT,               -- "NHL", "Olympic"
    capacity INTEGER,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (rink_id) REFERENCES rinks(rink_id)
);

-- =============================================================================
-- RULE CONFIGURATION
-- =============================================================================

-- Rule sets define league-specific configurations
CREATE TABLE IF NOT EXISTS rule_sets (
    rule_set_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,              -- "NHL Rules", "Youth U12", "Adult Rec"
    description TEXT,

    -- Game structure
    num_periods INTEGER DEFAULT 3,
    period_length_min INTEGER DEFAULT 20,
    intermission_length_min INTEGER DEFAULT 15,
    overtime_length_min INTEGER,     -- NULL if no overtime
    overtime_type TEXT,              -- "sudden_death", "full_period", NULL

    -- Gameplay rules
    icing_rule TEXT DEFAULT 'hybrid', -- "touch", "hybrid", "no_touch"
    offside_rule TEXT DEFAULT 'standard', -- "standard", "no_offside"
    body_checking INTEGER DEFAULT 1, -- 0 = no body checking (kids leagues)

    -- Point system for standings
    points_win INTEGER DEFAULT 2,
    points_loss INTEGER DEFAULT 0,
    points_tie INTEGER DEFAULT 1,
    points_otl INTEGER DEFAULT 1,    -- Overtime loss

    -- Roster rules
    max_roster_size INTEGER,
    min_players_to_start INTEGER,
    max_players_dressed INTEGER,

    created_at INTEGER NOT NULL
);

-- Infractions defined per rule set
CREATE TABLE IF NOT EXISTS rule_set_infractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_set_id TEXT NOT NULL,

    code TEXT NOT NULL,              -- "TRIP", "HOOK", "SLASH", "ROUGH"
    name TEXT NOT NULL,              -- "Tripping", "Hooking", "Slashing"
    description TEXT,

    -- Default penalty settings
    default_severity TEXT NOT NULL,  -- "minor", "major", "misconduct", etc.
    default_duration_min INTEGER NOT NULL,

    -- Can this infraction result in different severities?
    allows_minor INTEGER DEFAULT 1,
    allows_major INTEGER DEFAULT 0,
    allows_misconduct INTEGER DEFAULT 0,
    allows_match INTEGER DEFAULT 0,

    -- Is this infraction active for this rule set?
    is_active INTEGER DEFAULT 1,

    display_order INTEGER,

    UNIQUE(rule_set_id, code),
    FOREIGN KEY (rule_set_id) REFERENCES rule_sets(rule_set_id)
);

-- =============================================================================
-- TEMPORAL PARTICIPATION
-- =============================================================================

-- League operates during a season
CREATE TABLE IF NOT EXISTS league_seasons (
    league_id TEXT NOT NULL,
    season_id TEXT NOT NULL,
    rule_set_id TEXT,                -- Rules for this league+season
    is_active INTEGER DEFAULT 1,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (league_id, season_id),
    FOREIGN KEY (league_id) REFERENCES leagues(league_id),
    FOREIGN KEY (season_id) REFERENCES seasons(season_id),
    FOREIGN KEY (rule_set_id) REFERENCES rule_sets(rule_set_id)
);

-- Division active in a league+season
CREATE TABLE IF NOT EXISTS league_season_divisions (
    league_id TEXT NOT NULL,
    season_id TEXT NOT NULL,
    division_id TEXT NOT NULL,
    display_order INTEGER,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (league_id, season_id, division_id),
    FOREIGN KEY (league_id, season_id) REFERENCES league_seasons(league_id, season_id),
    FOREIGN KEY (division_id) REFERENCES divisions(division_id)
);

-- Division active in a tournament
CREATE TABLE IF NOT EXISTS tournament_divisions (
    tournament_id TEXT NOT NULL,
    division_id TEXT NOT NULL,
    display_order INTEGER,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (tournament_id, division_id),
    FOREIGN KEY (tournament_id) REFERENCES tournaments(tournament_id),
    FOREIGN KEY (division_id) REFERENCES divisions(division_id)
);

-- Team competing in a context = THE ROSTER
CREATE TABLE IF NOT EXISTS team_registrations (
    registration_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,

    -- Context: League+Season OR Tournament (mutually exclusive)
    league_id TEXT,
    season_id TEXT,
    tournament_id TEXT,

    -- Division within that context
    division_id TEXT NOT NULL,

    registered_at INTEGER NOT NULL,
    withdrawn_at INTEGER,            -- NULL if still active

    CHECK (
        (league_id IS NOT NULL AND season_id IS NOT NULL AND tournament_id IS NULL)
        OR (league_id IS NULL AND season_id IS NULL AND tournament_id IS NOT NULL)
    ),

    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (division_id) REFERENCES divisions(division_id)
);

-- Player on a team's roster for a period
CREATE TABLE IF NOT EXISTS roster_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_id TEXT NOT NULL,
    player_id INTEGER NOT NULL,

    jersey_number INTEGER,
    position TEXT,                   -- "C", "LW", "RW", "D", "G"
    roster_status TEXT DEFAULT 'active',  -- "active", "injured", "scratched"
    is_captain INTEGER DEFAULT 0,
    is_alternate INTEGER DEFAULT 0,

    added_at INTEGER NOT NULL,
    removed_at INTEGER,              -- NULL if still on roster

    FOREIGN KEY (registration_id) REFERENCES team_registrations(registration_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id)
);

-- Players available to sub
CREATE TABLE IF NOT EXISTS spare_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL,
    league_id TEXT NOT NULL,
    season_id TEXT NOT NULL,

    positions TEXT,                  -- JSON: ["C", "LW", "D"] or ["G"]
    skill_level TEXT,                -- "A", "B", "C"
    notes TEXT,

    is_active INTEGER DEFAULT 1,
    created_at INTEGER NOT NULL,

    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (league_id) REFERENCES leagues(league_id),
    FOREIGN KEY (season_id) REFERENCES seasons(season_id)
);

-- =============================================================================
-- PLAYOFFS
-- =============================================================================

CREATE TABLE IF NOT EXISTS playoff_brackets (
    bracket_id TEXT PRIMARY KEY,
    league_id TEXT NOT NULL,
    season_id TEXT NOT NULL,
    division_id TEXT,                -- NULL if league-wide

    name TEXT NOT NULL,
    format TEXT NOT NULL,            -- "single_elimination", "double_elimination"
    num_teams INTEGER,

    started_at INTEGER,
    completed_at INTEGER,
    champion_registration_id TEXT,

    created_at INTEGER NOT NULL,

    FOREIGN KEY (league_id) REFERENCES leagues(league_id),
    FOREIGN KEY (season_id) REFERENCES seasons(season_id)
);

CREATE TABLE IF NOT EXISTS playoff_series (
    series_id TEXT PRIMARY KEY,
    bracket_id TEXT NOT NULL,

    round INTEGER NOT NULL,          -- 1 = first round, 2 = semi, etc.
    series_number INTEGER,           -- Position in round

    higher_seed_registration_id TEXT,
    lower_seed_registration_id TEXT,

    format TEXT NOT NULL,            -- "single_game", "best_of_3", "best_of_5", "best_of_7"

    winner_registration_id TEXT,

    started_at INTEGER,
    completed_at INTEGER,

    FOREIGN KEY (bracket_id) REFERENCES playoff_brackets(bracket_id)
);

-- =============================================================================
-- GAMES
-- =============================================================================

CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,

    -- Venue
    rink_id TEXT NOT NULL,
    sheet_id TEXT,

    -- Teams (via registrations) - for normalized model
    home_registration_id TEXT,
    away_registration_id TEXT,

    -- Teams (direct) - for backwards compatibility with NHL loader
    home_team TEXT,
    away_team TEXT,
    home_abbrev TEXT,
    away_abbrev TEXT,

    -- Schedule
    scheduled_start TEXT,            -- ISO 8601 (new column name)
    start_time TEXT,                 -- ISO 8601 (legacy column name, same data)
    period_length_min INTEGER NOT NULL,
    num_periods INTEGER DEFAULT 3,

    -- Game metadata
    game_type TEXT DEFAULT 'regular', -- "regular", "playoff", "exhibition"
    game_status TEXT DEFAULT 'scheduled', -- "scheduled", "in_progress", "final", "postponed", "cancelled"

    -- Playoff context
    series_id TEXT,
    playoff_game_number INTEGER,

    created_at INTEGER NOT NULL,

    FOREIGN KEY (rink_id) REFERENCES rinks(rink_id),
    FOREIGN KEY (sheet_id) REFERENCES rink_sheets(sheet_id),
    FOREIGN KEY (home_registration_id) REFERENCES team_registrations(registration_id),
    FOREIGN KEY (away_registration_id) REFERENCES team_registrations(registration_id),
    FOREIGN KEY (series_id) REFERENCES playoff_series(series_id)
);

-- Officials assigned to games
CREATE TABLE IF NOT EXISTS game_officials (
    game_id TEXT NOT NULL,
    official_id TEXT NOT NULL,
    role TEXT NOT NULL,              -- "referee", "linesman", "scorekeeper"
    PRIMARY KEY (game_id, official_id),
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (official_id) REFERENCES officials(official_id)
);

-- =============================================================================
-- EVENTS (append-only log)
-- =============================================================================

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    event_type TEXT NOT NULL,

    -- When in the game
    period INTEGER,
    period_time_seconds INTEGER,     -- Time remaining in period
    game_time_seconds INTEGER,       -- Total elapsed game time

    -- Who
    team TEXT,                       -- "home" or "away"
    player_id INTEGER,
    assist1_id INTEGER,
    assist2_id INTEGER,

    -- Event-specific data
    payload TEXT,                    -- JSON

    created_at INTEGER NOT NULL,

    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id)
);

-- =============================================================================
-- DEVICE MANAGEMENT (kept from original schema)
-- =============================================================================

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    rink_id TEXT,
    sheet_name TEXT,
    device_name TEXT,
    is_assigned INTEGER DEFAULT 0,
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    notes TEXT,
    FOREIGN KEY (rink_id) REFERENCES rinks(rink_id)
);

-- Legacy team_rosters table for backwards compatibility with NHL loader
CREATE TABLE IF NOT EXISTS team_rosters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL,
    team_abbrev TEXT NOT NULL,
    roster_status TEXT NOT NULL,
    added_at INTEGER NOT NULL,
    removed_at INTEGER,
    UNIQUE(player_id, team_abbrev, added_at),
    FOREIGN KEY (player_id) REFERENCES players(player_id)
);

CREATE TABLE IF NOT EXISTS received_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    ts_local TEXT NOT NULL,
    payload TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    current_game_id TEXT,
    game_state TEXT,
    clock_running INTEGER,
    clock_value_ms INTEGER,
    last_event_seq INTEGER,
    app_version TEXT,
    ts_local TEXT NOT NULL,
    received_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_versions (
    rink_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (rink_id) REFERENCES rinks(rink_id)
);
"""

# =============================================================================
# Indexes
# =============================================================================

INDEXES = """
-- Divisions
CREATE INDEX IF NOT EXISTS idx_divisions_parent ON divisions(parent_division_id);

-- Rink sheets
CREATE INDEX IF NOT EXISTS idx_rink_sheets_rink ON rink_sheets(rink_id);

-- Rule set infractions
CREATE INDEX IF NOT EXISTS idx_infractions_rule_set ON rule_set_infractions(rule_set_id);

-- Team registrations
CREATE INDEX IF NOT EXISTS idx_team_reg_league_season ON team_registrations(league_id, season_id);
CREATE INDEX IF NOT EXISTS idx_team_reg_tournament ON team_registrations(tournament_id);
CREATE INDEX IF NOT EXISTS idx_team_reg_division ON team_registrations(division_id);

-- Roster entries
CREATE INDEX IF NOT EXISTS idx_roster_registration ON roster_entries(registration_id);
CREATE INDEX IF NOT EXISTS idx_roster_player ON roster_entries(player_id);

-- Spare players
CREATE INDEX IF NOT EXISTS idx_spare_league_season ON spare_players(league_id, season_id);

-- Games
CREATE INDEX IF NOT EXISTS idx_games_schedule ON games(scheduled_start);
CREATE INDEX IF NOT EXISTS idx_games_registrations ON games(home_registration_id, away_registration_id);
CREATE INDEX IF NOT EXISTS idx_games_rink ON games(rink_id);

-- Events
CREATE INDEX IF NOT EXISTS idx_events_game ON events(game_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_player ON events(player_id);

-- Received events
CREATE INDEX IF NOT EXISTS idx_received_events_game ON received_events(game_id);
CREATE INDEX IF NOT EXISTS idx_received_events_event_id ON received_events(event_id);

-- Heartbeats
CREATE INDEX IF NOT EXISTS idx_heartbeats_device ON heartbeats(device_id, received_at DESC);

-- Playoff brackets
CREATE INDEX IF NOT EXISTS idx_brackets_league_season ON playoff_brackets(league_id, season_id);

-- Playoff series
CREATE INDEX IF NOT EXISTS idx_series_bracket ON playoff_series(bracket_id);
"""

# =============================================================================
# Default Data
# =============================================================================

DEFAULT_RULE_SETS = [
    {
        "rule_set_id": "nhl",
        "name": "NHL Rules",
        "description": "Standard NHL rules",
        "num_periods": 3,
        "period_length_min": 20,
        "intermission_length_min": 18,
        "overtime_length_min": 5,
        "overtime_type": "sudden_death",
        "icing_rule": "hybrid",
        "offside_rule": "standard",
        "body_checking": 1,
        "points_win": 2,
        "points_loss": 0,
        "points_tie": 0,
        "points_otl": 1,
        "max_roster_size": 23,
        "min_players_to_start": 6,
        "max_players_dressed": 20,
    },
    {
        "rule_set_id": "adult-rec",
        "name": "Adult Recreational",
        "description": "Standard adult rec league rules - no checking",
        "num_periods": 3,
        "period_length_min": 15,
        "intermission_length_min": 5,
        "overtime_length_min": 5,
        "overtime_type": "sudden_death",
        "icing_rule": "hybrid",
        "offside_rule": "standard",
        "body_checking": 0,
        "points_win": 2,
        "points_loss": 0,
        "points_tie": 1,
        "points_otl": 1,
        "max_roster_size": 25,
        "min_players_to_start": 6,
        "max_players_dressed": 16,
    },
    {
        "rule_set_id": "youth-u12",
        "name": "Youth U12",
        "description": "Youth under-12 rules",
        "num_periods": 3,
        "period_length_min": 12,
        "intermission_length_min": 3,
        "overtime_length_min": None,
        "overtime_type": None,
        "icing_rule": "no_touch",
        "offside_rule": "standard",
        "body_checking": 0,
        "points_win": 2,
        "points_loss": 0,
        "points_tie": 1,
        "points_otl": 0,
        "max_roster_size": 20,
        "min_players_to_start": 6,
        "max_players_dressed": 15,
    },
]

DEFAULT_INFRACTIONS = {
    "nhl": [
        ("TRIP", "Tripping", "minor", 2, 1, 0, 0, 0, 1),
        ("HOOK", "Hooking", "minor", 2, 1, 0, 0, 0, 2),
        ("HOLD", "Holding", "minor", 2, 1, 0, 0, 0, 3),
        ("SLASH", "Slashing", "minor", 2, 1, 1, 0, 0, 4),
        ("INTRF", "Interference", "minor", 2, 1, 0, 0, 0, 5),
        ("ROUGH", "Roughing", "minor", 2, 1, 1, 0, 0, 6),
        ("HSTCK", "High Sticking", "minor", 2, 1, 1, 0, 0, 7),
        ("CROSS", "Cross Checking", "minor", 2, 1, 1, 0, 0, 8),
        ("ELBOW", "Elbowing", "minor", 2, 1, 1, 0, 0, 9),
        ("BOARD", "Boarding", "minor", 2, 1, 1, 0, 0, 10),
        ("CHARG", "Charging", "minor", 2, 1, 1, 0, 0, 11),
        ("DELAY", "Delay of Game", "minor", 2, 1, 0, 0, 0, 12),
        ("TMPEN", "Too Many Men", "minor", 2, 1, 0, 0, 0, 13),
        ("UNSPT", "Unsportsmanlike", "minor", 2, 1, 0, 1, 0, 14),
        ("MISCD", "Misconduct", "misconduct", 10, 0, 0, 1, 0, 15),
        ("GMCON", "Game Misconduct", "game_misconduct", 0, 0, 0, 0, 0, 16),
        ("FIGHT", "Fighting", "major", 5, 0, 1, 1, 0, 17),
    ],
    "adult-rec": [
        ("TRIP", "Tripping", "minor", 2, 1, 0, 0, 0, 1),
        ("HOOK", "Hooking", "minor", 2, 1, 0, 0, 0, 2),
        ("HOLD", "Holding", "minor", 2, 1, 0, 0, 0, 3),
        ("SLASH", "Slashing", "minor", 2, 1, 1, 0, 0, 4),
        ("INTRF", "Interference", "minor", 2, 1, 0, 0, 0, 5),
        ("ROUGH", "Roughing", "minor", 2, 1, 1, 0, 0, 6),
        ("HSTCK", "High Sticking", "minor", 2, 1, 1, 0, 0, 7),
        ("CROSS", "Cross Checking", "minor", 2, 1, 1, 0, 0, 8),
        ("ELBOW", "Elbowing", "minor", 2, 1, 1, 0, 0, 9),
        ("BOARD", "Boarding", "minor", 2, 1, 1, 0, 0, 10),
        ("DELAY", "Delay of Game", "minor", 2, 1, 0, 0, 0, 11),
        ("TMPEN", "Too Many Men", "minor", 2, 1, 0, 0, 0, 12),
        ("UNSPT", "Unsportsmanlike", "minor", 2, 1, 0, 1, 0, 13),
        ("MISCD", "Misconduct", "misconduct", 10, 0, 0, 1, 0, 14),
        ("GMCON", "Game Misconduct", "game_misconduct", 0, 0, 0, 0, 0, 15),
        ("FIGHT", "Fighting", "major", 5, 0, 1, 1, 0, 16),
    ],
    "youth-u12": [
        ("TRIP", "Tripping", "minor", 1, 1, 0, 0, 0, 1),
        ("HOOK", "Hooking", "minor", 1, 1, 0, 0, 0, 2),
        ("HOLD", "Holding", "minor", 1, 1, 0, 0, 0, 3),
        ("SLASH", "Slashing", "minor", 1, 1, 0, 0, 0, 4),
        ("INTRF", "Interference", "minor", 1, 1, 0, 0, 0, 5),
        ("HSTCK", "High Sticking", "minor", 1, 1, 0, 0, 0, 6),
        ("DELAY", "Delay of Game", "minor", 1, 1, 0, 0, 0, 7),
        ("TMPEN", "Too Many Men", "minor", 1, 1, 0, 0, 0, 8),
        ("UNSPT", "Unsportsmanlike", "minor", 2, 1, 0, 1, 0, 9),
    ],
}


def init_schema(db_path: str, fresh_start: bool = False) -> None:
    """
    Initialize the cloud database schema.

    Args:
        db_path: Path to the SQLite database file
        fresh_start: If True, drop all tables and start fresh
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        if fresh_start:
            logger.info("Fresh start: dropping all existing tables")
            # Get all table names
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            for table in tables:
                if table["name"] != "sqlite_sequence":
                    conn.execute(f"DROP TABLE IF EXISTS {table['name']}")
            conn.commit()

        # Create all tables
        logger.info("Creating tables...")
        conn.executescript(TABLES)
        conn.commit()

        # Create indexes
        logger.info("Creating indexes...")
        conn.executescript(INDEXES)
        conn.commit()

        # Seed default rule sets if empty
        count = conn.execute("SELECT COUNT(*) FROM rule_sets").fetchone()[0]
        if count == 0:
            logger.info("Seeding default rule sets...")
            _seed_rule_sets(conn)
            conn.commit()

        logger.info(f"Schema initialized (version {SCHEMA_VERSION})")

    finally:
        conn.close()


def _seed_rule_sets(conn: sqlite3.Connection) -> None:
    """Seed default rule sets and infractions."""
    now = int(time.time())

    for rule_set in DEFAULT_RULE_SETS:
        conn.execute(
            """
            INSERT INTO rule_sets (
                rule_set_id, name, description,
                num_periods, period_length_min, intermission_length_min,
                overtime_length_min, overtime_type,
                icing_rule, offside_rule, body_checking,
                points_win, points_loss, points_tie, points_otl,
                max_roster_size, min_players_to_start, max_players_dressed,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule_set["rule_set_id"],
                rule_set["name"],
                rule_set["description"],
                rule_set["num_periods"],
                rule_set["period_length_min"],
                rule_set["intermission_length_min"],
                rule_set["overtime_length_min"],
                rule_set["overtime_type"],
                rule_set["icing_rule"],
                rule_set["offside_rule"],
                rule_set["body_checking"],
                rule_set["points_win"],
                rule_set["points_loss"],
                rule_set["points_tie"],
                rule_set["points_otl"],
                rule_set["max_roster_size"],
                rule_set["min_players_to_start"],
                rule_set["max_players_dressed"],
                now,
            ),
        )

        # Seed infractions for this rule set
        infractions = DEFAULT_INFRACTIONS.get(rule_set["rule_set_id"], [])
        for infraction in infractions:
            conn.execute(
                """
                INSERT INTO rule_set_infractions (
                    rule_set_id, code, name, default_severity, default_duration_min,
                    allows_minor, allows_major, allows_misconduct, allows_match,
                    display_order
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rule_set["rule_set_id"], *infraction),
            )


def get_schema_version(db_path: str) -> str | None:  # noqa: ARG001
    """Get the current schema version from the database."""
    # For now, we just return the code version
    # In the future, we could store this in a metadata table
    return SCHEMA_VERSION
