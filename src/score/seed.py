"""
Database seeding functions for Score.

This module provides functions to populate the cloud database with sample data
for development and testing purposes.
"""

import random
import sqlite3
import time
from datetime import datetime, timedelta

# ---------- Sample Data ----------

SAMPLE_LEAGUES = [
    {
        "league_id": "nhl",
        "name": "National Hockey League",
        "league_type": "professional",
        "description": "Professional hockey league",
        "website": "https://nhl.com",
    },
    {
        "league_id": "baal",
        "name": "Bay Area Adult League",
        "league_type": "rec",
        "description": "Adult recreational hockey league",
    },
]

SAMPLE_SEASONS = [
    {
        "season_id": "2024-2025",
        "name": "2024-2025 Season",
        "start_date": "2024-10-01",
        "end_date": "2025-04-30",
    },
    {
        "season_id": "2025-2026",
        "name": "2025-2026 Season",
        "start_date": "2025-10-01",
        "end_date": "2026-04-30",
    },
]

SAMPLE_DIVISIONS = [
    # NHL divisions
    {"division_id": "atlantic", "name": "Atlantic Division", "division_type": "division"},
    {"division_id": "pacific", "name": "Pacific Division", "division_type": "division"},
    # Rec league divisions
    {"division_id": "div-a", "name": "A Division", "division_type": "division"},
    {"division_id": "div-b", "name": "B Division", "division_type": "division"},
]

SAMPLE_RINKS = [
    {
        "rink_id": "sharks-ice",
        "name": "Sharks Ice at San Jose",
        "address": "1500 S 10th St",
        "city": "San Jose",
        "province_state": "CA",
        "postal_code": "95112",
        "country": "USA",
    },
    {
        "rink_id": "oakland-ice",
        "name": "Oakland Ice Center",
        "address": "519 18th St",
        "city": "Oakland",
        "province_state": "CA",
        "postal_code": "94612",
        "country": "USA",
    },
]

SAMPLE_RINK_SHEETS = [
    {"sheet_id": "sharks-ice-a", "rink_id": "sharks-ice", "name": "Sheet A", "surface_type": "NHL"},
    {"sheet_id": "sharks-ice-b", "rink_id": "sharks-ice", "name": "Sheet B", "surface_type": "NHL"},
    {"sheet_id": "oakland-ice-main", "rink_id": "oakland-ice", "name": "Main Rink", "surface_type": "NHL"},
    {"sheet_id": "oakland-ice-studio", "rink_id": "oakland-ice", "name": "Studio Rink", "surface_type": "NHL"},
]

SAMPLE_TEAMS = [
    # NHL teams
    {"team_id": "sjs", "name": "Sharks", "city": "San Jose", "abbreviation": "SJS", "team_type": "franchise"},
    {"team_id": "lak", "name": "Kings", "city": "Los Angeles", "abbreviation": "LAK", "team_type": "franchise"},
    {"team_id": "ana", "name": "Ducks", "city": "Anaheim", "abbreviation": "ANA", "team_type": "franchise"},
    {"team_id": "vgk", "name": "Golden Knights", "city": "Las Vegas", "abbreviation": "VGK", "team_type": "franchise"},
    # Rec league teams
    {"team_id": "ice-dogs", "name": "Ice Dogs", "abbreviation": "DOG", "team_type": "club"},
    {"team_id": "polar-bears", "name": "Polar Bears", "abbreviation": "PBR", "team_type": "club"},
    {"team_id": "frozen-fury", "name": "Frozen Fury", "abbreviation": "FRZ", "team_type": "club"},
    {"team_id": "chill-factor", "name": "Chill Factor", "abbreviation": "CHL", "team_type": "club"},
]

# Player name pools for generation
FIRST_NAMES = [
    "Alex", "Brandon", "Chris", "David", "Eric", "Frank", "Greg", "Henry",
    "Ian", "Jake", "Kevin", "Luke", "Mike", "Nick", "Owen", "Paul",
    "Quinn", "Ryan", "Steve", "Tom", "Victor", "Will", "Xavier", "Zach",
    "Adam", "Ben", "Connor", "Dylan", "Ethan", "Finn", "Gavin", "Hunter",
    "Jack", "Kyle", "Logan", "Matt", "Nathan", "Oscar", "Peter", "Robert",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Anderson", "Taylor", "Thomas", "Moore", "Jackson",
    "Martin", "Lee", "Thompson", "White", "Harris", "Clark", "Lewis", "Robinson",
    "Walker", "Hall", "Young", "King", "Wright", "Scott", "Green", "Baker",
    "Adams", "Nelson", "Hill", "Campbell", "Mitchell", "Roberts", "Carter", "Phillips",
]

POSITIONS = ["C", "LW", "RW", "D", "D", "G"]  # Weighted for realistic distribution


# ---------- Seeding Functions ----------

def seed_leagues(conn: sqlite3.Connection) -> int:
    """Seed sample leagues."""
    now = int(time.time())
    count = 0
    for league in SAMPLE_LEAGUES:
        try:
            conn.execute("""
                INSERT INTO leagues (league_id, name, league_type, description, website, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                league["league_id"],
                league["name"],
                league.get("league_type"),
                league.get("description"),
                league.get("website"),
                now,
            ))
            count += 1
        except sqlite3.IntegrityError:
            pass  # Already exists
    return count


def seed_seasons(conn: sqlite3.Connection) -> int:
    """Seed sample seasons."""
    now = int(time.time())
    count = 0
    for season in SAMPLE_SEASONS:
        try:
            conn.execute("""
                INSERT INTO seasons (season_id, name, start_date, end_date, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                season["season_id"],
                season["name"],
                season["start_date"],
                season.get("end_date"),
                now,
            ))
            count += 1
        except sqlite3.IntegrityError:
            pass
    return count


def seed_divisions(conn: sqlite3.Connection) -> int:
    """Seed sample divisions."""
    now = int(time.time())
    count = 0
    for div in SAMPLE_DIVISIONS:
        try:
            conn.execute("""
                INSERT INTO divisions (division_id, name, division_type, created_at)
                VALUES (?, ?, ?, ?)
            """, (
                div["division_id"],
                div["name"],
                div.get("division_type"),
                now,
            ))
            count += 1
        except sqlite3.IntegrityError:
            pass
    return count


def seed_rinks(conn: sqlite3.Connection) -> int:
    """Seed sample rinks and their sheets."""
    now = int(time.time())
    rink_count = 0
    sheet_count = 0

    for rink in SAMPLE_RINKS:
        try:
            conn.execute("""
                INSERT INTO rinks (rink_id, name, address, city, province_state, postal_code, country, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rink["rink_id"],
                rink["name"],
                rink.get("address"),
                rink.get("city"),
                rink.get("province_state"),
                rink.get("postal_code"),
                rink.get("country"),
                now,
            ))
            rink_count += 1
        except sqlite3.IntegrityError:
            pass

    for sheet in SAMPLE_RINK_SHEETS:
        try:
            conn.execute("""
                INSERT INTO rink_sheets (sheet_id, rink_id, name, surface_type, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                sheet["sheet_id"],
                sheet["rink_id"],
                sheet["name"],
                sheet.get("surface_type"),
                now,
            ))
            sheet_count += 1
        except sqlite3.IntegrityError:
            pass

    return rink_count


def seed_teams(conn: sqlite3.Connection) -> int:
    """Seed sample teams."""
    now = int(time.time())
    count = 0
    for team in SAMPLE_TEAMS:
        try:
            conn.execute("""
                INSERT INTO teams (team_id, name, city, abbreviation, team_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                team["team_id"],
                team["name"],
                team.get("city"),
                team.get("abbreviation"),
                team.get("team_type"),
                now,
            ))
            count += 1
        except sqlite3.IntegrityError:
            pass
    return count


def seed_players(conn: sqlite3.Connection, count: int = 120) -> int:
    """Seed sample players with random names."""
    now = int(time.time())
    created = 0

    # Start player IDs from 1001 to avoid conflicts
    for i in range(count):
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        full_name = f"{first} {last}"
        shoots = random.choice(["L", "R"])

        try:
            conn.execute("""
                INSERT INTO players (player_id, first_name, last_name, full_name, shoots_catches, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                1001 + i,
                first,
                last,
                full_name,
                shoots,
                now,
            ))
            created += 1
        except sqlite3.IntegrityError:
            pass

    return created


def seed_league_seasons(conn: sqlite3.Connection) -> int:
    """Link leagues to seasons with rule sets."""
    now = int(time.time())
    count = 0

    # NHL uses NHL rules, rec league uses adult-rec rules
    links = [
        ("nhl", "2024-2025", "nhl"),
        ("nhl", "2025-2026", "nhl"),
        ("baal", "2024-2025", "adult-rec"),
        ("baal", "2025-2026", "adult-rec"),
    ]

    for league_id, season_id, rule_set_id in links:
        try:
            conn.execute("""
                INSERT INTO league_seasons (league_id, season_id, rule_set_id, is_active, created_at)
                VALUES (?, ?, ?, 1, ?)
            """, (league_id, season_id, rule_set_id, now))
            count += 1
        except sqlite3.IntegrityError:
            pass

    return count


def seed_registrations(conn: sqlite3.Connection) -> int:
    """Register teams in leagues for the current season."""
    now = int(time.time())
    count = 0

    # NHL teams in Pacific division, rec teams split between A and B
    registrations = [
        # NHL Pacific teams
        ("reg-sjs-2025", "sjs", "nhl", "2025-2026", "pacific"),
        ("reg-lak-2025", "lak", "nhl", "2025-2026", "pacific"),
        ("reg-ana-2025", "ana", "nhl", "2025-2026", "pacific"),
        ("reg-vgk-2025", "vgk", "nhl", "2025-2026", "pacific"),
        # Rec league teams
        ("reg-dogs-2025", "ice-dogs", "baal", "2025-2026", "div-a"),
        ("reg-bears-2025", "polar-bears", "baal", "2025-2026", "div-a"),
        ("reg-fury-2025", "frozen-fury", "baal", "2025-2026", "div-b"),
        ("reg-chill-2025", "chill-factor", "baal", "2025-2026", "div-b"),
    ]

    for reg_id, team_id, league_id, season_id, division_id in registrations:
        try:
            conn.execute("""
                INSERT INTO team_registrations (registration_id, team_id, league_id, season_id, division_id, registered_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (reg_id, team_id, league_id, season_id, division_id, now))
            count += 1
        except sqlite3.IntegrityError:
            pass

    return count


def seed_rosters(conn: sqlite3.Connection) -> int:
    """Add players to team rosters."""
    now = int(time.time())
    count = 0

    # Get all registrations
    registrations = conn.execute("SELECT registration_id FROM team_registrations").fetchall()

    # Get all players
    players = conn.execute("SELECT player_id FROM players ORDER BY player_id").fetchall()

    if not players or not registrations:
        return 0

    # Distribute players across teams (15 per team)
    players_per_team = len(players) // len(registrations)
    player_idx = 0

    for reg in registrations:
        reg_id = reg["registration_id"]

        for jersey_num in range(1, min(players_per_team + 1, 16)):
            if player_idx >= len(players):
                break

            player_id = players[player_idx]["player_id"]
            position = random.choice(POSITIONS)

            try:
                conn.execute("""
                    INSERT INTO roster_entries (registration_id, player_id, jersey_number, position, roster_status, added_at)
                    VALUES (?, ?, ?, ?, 'active', ?)
                """, (reg_id, player_id, jersey_num, position, now))
                count += 1
            except sqlite3.IntegrityError:
                pass

            player_idx += 1

    return count


def seed_games(conn: sqlite3.Connection, game_count: int = 8) -> int:
    """Create sample games for today and tomorrow."""
    now = int(time.time())
    count = 0

    # Get rinks and sheets
    sheets = conn.execute("SELECT sheet_id, rink_id FROM rink_sheets").fetchall()
    if not sheets:
        return 0

    # Get registrations with team info for pairing
    regs = conn.execute("""
        SELECT tr.registration_id, tr.team_id, t.name, t.abbreviation
        FROM team_registrations tr
        JOIN teams t ON tr.team_id = t.team_id
    """).fetchall()
    if len(regs) < 2:
        return 0

    # Create games
    today = datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)

    for i in range(game_count):
        game_id = f"game-{int(time.time())}-{i}"

        # Alternate between days
        game_date = today + timedelta(days=i % 2, hours=(i // 2) * 2)
        start_time = game_date.isoformat()

        # Pick sheet
        sheet = sheets[i % len(sheets)]

        # Pick teams (home vs away)
        home_idx = (i * 2) % len(regs)
        away_idx = (i * 2 + 1) % len(regs)
        home_reg = regs[home_idx]
        away_reg = regs[away_idx]

        try:
            conn.execute("""
                INSERT INTO games (
                    game_id, rink_id, sheet_id, home_registration_id, away_registration_id,
                    home_team, away_team, home_abbrev, away_abbrev,
                    scheduled_start, start_time, period_length_min, num_periods, game_type, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 3, 'regular', ?)
            """, (
                game_id,
                sheet["rink_id"],
                sheet["sheet_id"],
                home_reg["registration_id"],
                away_reg["registration_id"],
                home_reg["name"],
                away_reg["name"],
                home_reg["abbreviation"],
                away_reg["abbreviation"],
                start_time,
                start_time,
                15,  # 15 min periods for rec games
                now,
            ))
            count += 1
        except sqlite3.IntegrityError:
            pass

    return count


def clear_all(conn: sqlite3.Connection) -> dict:
    """Clear all seeded data (preserving rule_sets)."""
    tables = [
        "games",
        "roster_entries",
        "team_registrations",
        "league_seasons",
        "players",
        "teams",
        "rink_sheets",
        "rinks",
        "divisions",
        "seasons",
        "leagues",
    ]

    counts = {}
    for table in tables:
        try:
            result = conn.execute(f"DELETE FROM {table}")
            counts[table] = result.rowcount
        except sqlite3.OperationalError:
            counts[table] = 0

    return counts


def seed_all(db_path: str, player_count: int = 120, game_count: int = 8) -> dict:
    """Seed all sample data in dependency order."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    results = {}

    results["leagues"] = seed_leagues(conn)
    results["seasons"] = seed_seasons(conn)
    results["divisions"] = seed_divisions(conn)
    results["rinks"] = seed_rinks(conn)
    results["teams"] = seed_teams(conn)
    results["players"] = seed_players(conn, player_count)
    results["league_seasons"] = seed_league_seasons(conn)
    results["registrations"] = seed_registrations(conn)
    results["rosters"] = seed_rosters(conn)
    results["games"] = seed_games(conn, game_count)

    conn.commit()
    conn.close()

    return results
