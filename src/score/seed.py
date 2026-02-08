"""
Database seeding functions for Score.

This module provides functions to populate the cloud database with sample data
for development and testing purposes.
"""

import logging
import random
import sqlite3
import time
from datetime import datetime, timedelta

logger = logging.getLogger("score.seed")

# ---------- Default Client ----------

DEFAULT_CLIENT_ID = "default"
DEFAULT_CLIENT = {
    "client_id": DEFAULT_CLIENT_ID,
    "name": "Default Client",
    "slug": "default",
    "contact_email": "admin@example.com",
}

# ---------- Sample Data ----------

SAMPLE_LEAGUES = [
    {
        "league_id": "tspc",
        "name": "TSPC Adult Hockey League",
        "league_type": "rec",
        "description": "Adult recreational hockey league at Toyota Sports Performance Center",
    },
]

SAMPLE_SEASONS = [
    {
        "season_id": "fall-2025",
        "name": "Fall 2025",
        "start_date": "2025-09-01",
        "end_date": "2025-11-30",
    },
    {
        "season_id": "winter-2026",
        "name": "Winter 2026",
        "start_date": "2025-12-01",
        "end_date": "2026-02-28",
    },
    {
        "season_id": "spring-2026",
        "name": "Spring 2026",
        "start_date": "2026-03-01",
        "end_date": "2026-05-31",
    },
    {
        "season_id": "summer-2026",
        "name": "Summer 2026",
        "start_date": "2026-06-01",
        "end_date": "2026-08-31",
    },
]

SAMPLE_DIVISIONS = [
    {"division_id": "bronze-a", "name": "Bronze A", "division_type": "division"},
    {"division_id": "bronze-aa", "name": "Bronze AA", "division_type": "division"},
    {"division_id": "bronze-aaa", "name": "Bronze AAA", "division_type": "division"},
    {"division_id": "silver-a", "name": "Silver A", "division_type": "division"},
    {"division_id": "silver-aa", "name": "Silver AA", "division_type": "division"},
    {"division_id": "gold", "name": "Gold", "division_type": "division"},
]

SAMPLE_RINKS = [
    {
        "rink_id": "tspc",
        "name": "Toyota Sports Performance Center",
        "address": "555 N Nash St",
        "city": "El Segundo",
        "province_state": "CA",
        "postal_code": "90245",
        "country": "USA",
    },
]

SAMPLE_RINK_SHEETS = [
    {"sheet_id": "tspc-pond", "rink_id": "tspc", "name": "Pond", "surface_type": "NHL"},
    {"sheet_id": "tspc-nhl", "rink_id": "tspc", "name": "NHL", "surface_type": "NHL"},
    {"sheet_id": "tspc-olympic", "rink_id": "tspc", "name": "Olympic", "surface_type": "Olympic"},
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

def seed_client(conn: sqlite3.Connection, client_id: str = DEFAULT_CLIENT_ID) -> int:
    """Seed the default client or create a specific client."""
    now = int(time.time())
    client = DEFAULT_CLIENT if client_id == DEFAULT_CLIENT_ID else {
        "client_id": client_id,
        "name": f"Client {client_id}",
        "slug": client_id,
        "contact_email": f"admin@{client_id}.example.com",
    }

    try:
        conn.execute("""
            INSERT INTO clients (client_id, name, slug, contact_email, is_active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
        """, (
            client["client_id"],
            client["name"],
            client["slug"],
            client["contact_email"],
            now,
        ))
        logger.info(f"Created client: {client['name']} ({client['client_id']})")
        return 1
    except sqlite3.IntegrityError:
        return 0  # Already exists


def seed_leagues(conn: sqlite3.Connection, client_id: str = DEFAULT_CLIENT_ID) -> int:
    """Seed sample leagues."""
    now = int(time.time())
    count = 0
    for league in SAMPLE_LEAGUES:
        try:
            conn.execute("""
                INSERT INTO leagues (client_id, league_id, name, league_type, description, website, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                client_id,
                league["league_id"],
                league["name"],
                league.get("league_type"),
                league.get("description"),
                league.get("website"),
                now,
            ))
            logger.info(f"Created league: {league['name']} ({league['league_id']})")
            count += 1
        except sqlite3.IntegrityError:
            pass  # Already exists
    return count


def seed_seasons(conn: sqlite3.Connection, client_id: str = DEFAULT_CLIENT_ID) -> int:
    """Seed sample seasons."""
    now = int(time.time())
    count = 0
    for season in SAMPLE_SEASONS:
        try:
            conn.execute("""
                INSERT INTO seasons (client_id, season_id, name, start_date, end_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                client_id,
                season["season_id"],
                season["name"],
                season["start_date"],
                season.get("end_date"),
                now,
            ))
            logger.info(f"Created season: {season['name']} ({season['start_date']} to {season.get('end_date', 'ongoing')})")
            count += 1
        except sqlite3.IntegrityError:
            pass
    return count


def seed_divisions(conn: sqlite3.Connection, client_id: str = DEFAULT_CLIENT_ID) -> int:
    """Seed sample divisions."""
    now = int(time.time())
    count = 0
    for div in SAMPLE_DIVISIONS:
        try:
            conn.execute("""
                INSERT INTO divisions (client_id, division_id, name, division_type, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                client_id,
                div["division_id"],
                div["name"],
                div.get("division_type"),
                now,
            ))
            logger.info(f"Created division: {div['name']}")
            count += 1
        except sqlite3.IntegrityError:
            pass
    return count


def seed_rinks(conn: sqlite3.Connection, client_id: str = DEFAULT_CLIENT_ID) -> int:
    """Seed sample rinks and their sheets."""
    now = int(time.time())
    rink_count = 0
    sheet_count = 0

    for rink in SAMPLE_RINKS:
        try:
            conn.execute("""
                INSERT INTO rinks (client_id, rink_id, name, address, city, province_state, postal_code, country, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                client_id,
                rink["rink_id"],
                rink["name"],
                rink.get("address"),
                rink.get("city"),
                rink.get("province_state"),
                rink.get("postal_code"),
                rink.get("country"),
                now,
            ))
            location = f"{rink.get('city', '')}, {rink.get('province_state', '')}".strip(", ")
            logger.info(f"Created venue: {rink['name']} ({location})")
            rink_count += 1
        except sqlite3.IntegrityError:
            pass

    for sheet in SAMPLE_RINK_SHEETS:
        try:
            conn.execute("""
                INSERT INTO rink_sheets (client_id, rink_id, sheet_id, name, surface_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                client_id,
                sheet["rink_id"],
                sheet["sheet_id"],
                sheet["name"],
                sheet.get("surface_type"),
                now,
            ))
            logger.info(f"  - Sheet: {sheet['name']} ({sheet.get('surface_type', 'standard')})")
            sheet_count += 1
        except sqlite3.IntegrityError:
            pass

    return rink_count


def seed_players(conn: sqlite3.Connection, count: int = 120, client_id: str = DEFAULT_CLIENT_ID) -> int:
    """Seed sample players with random names."""
    now = int(time.time())
    created = 0
    sample_names = []

    # Start player IDs from 1001 to avoid conflicts
    for i in range(count):
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        full_name = f"{first} {last}"
        shoots = random.choice(["L", "R"])

        try:
            conn.execute("""
                INSERT INTO players (client_id, player_id, first_name, last_name, full_name, shoots_catches, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                client_id,
                1001 + i,
                first,
                last,
                full_name,
                shoots,
                now,
            ))
            created += 1
            if len(sample_names) < 5:
                sample_names.append(full_name)
        except sqlite3.IntegrityError:
            pass

    if created > 0:
        names_preview = ", ".join(sample_names)
        if created > 5:
            names_preview += f", ... and {created - 5} more"
        logger.info(f"Created {created} players: {names_preview}")

    return created


def seed_league_seasons(conn: sqlite3.Connection, client_id: str = DEFAULT_CLIENT_ID) -> int:
    """Link leagues to seasons with rule sets."""
    now = int(time.time())
    count = 0

    # TSPC uses adult-rec rules for all seasons
    links = [
        (client_id, "tspc", "fall-2025", "adult-rec"),
        (client_id, "tspc", "winter-2026", "adult-rec"),
        (client_id, "tspc", "spring-2026", "adult-rec"),
        (client_id, "tspc", "summer-2026", "adult-rec"),
    ]

    for cid, league_id, season_id, rule_set_id in links:
        try:
            conn.execute("""
                INSERT INTO league_seasons (client_id, league_id, season_id, rule_set_id, is_active, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
            """, (cid, league_id, season_id, rule_set_id, now))
            count += 1
        except sqlite3.IntegrityError:
            pass

    return count


def seed_league_season_divisions(conn: sqlite3.Connection, client_id: str = DEFAULT_CLIENT_ID) -> int:
    """Link divisions to league-seasons."""
    now = int(time.time())
    count = 0

    # Link all divisions to all seasons for TSPC
    seasons = ["fall-2025", "winter-2026", "spring-2026", "summer-2026"]
    divisions = [
        ("bronze-a", 1),
        ("bronze-aa", 2),
        ("bronze-aaa", 3),
        ("silver-a", 4),
        ("silver-aa", 5),
        ("gold", 6),
    ]

    for season_id in seasons:
        for division_id, display_order in divisions:
            try:
                conn.execute("""
                    INSERT INTO league_season_divisions (client_id, league_id, season_id, division_id, display_order, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (client_id, "tspc", season_id, division_id, display_order, now))
                count += 1
            except sqlite3.IntegrityError:
                pass

    return count


def seed_registrations(conn: sqlite3.Connection, client_id: str = DEFAULT_CLIENT_ID) -> int:
    """Register teams in leagues for the current season."""
    now = int(time.time())
    count = 0

    # Team registrations for Winter 2026 season across different divisions
    # Format: (reg_id, team_name, abbreviation, league_id, season_id, division_id, organizer_name, organizer_email)
    registrations = [
        # Bronze A teams
        ("reg-dogs-w26", "Ice Dogs", "DOG", "tspc", "winter-2026", "bronze-a", "John Smith", "john@icedogs.com"),
        ("reg-bears-w26", "Polar Bears", "PBR", "tspc", "winter-2026", "bronze-a", "Jane Doe", "jane@polarbears.com"),
        ("reg-fury-w26", "Frozen Fury", "FRZ", "tspc", "winter-2026", "bronze-a", "Mike Johnson", "mike@frozenfury.com"),
        ("reg-chill-w26", "Chill Factor", "CHL", "tspc", "winter-2026", "bronze-a", "Sarah Williams", "sarah@chillfactor.com"),
        # Bronze AA teams
        ("reg-thunder-w26", "Thunder Bay", "THB", "tspc", "winter-2026", "bronze-aa", "Tom Brown", "tom@thunderbay.com"),
        ("reg-storm-w26", "Ice Storm", "STM", "tspc", "winter-2026", "bronze-aa", "Lisa Chen", "lisa@icestorm.com"),
        # Silver A teams
        ("reg-hawks-w26", "Night Hawks", "NHK", "tspc", "winter-2026", "silver-a", "David Lee", "david@nighthawks.com"),
        ("reg-wolves-w26", "Timber Wolves", "TWF", "tspc", "winter-2026", "silver-a", "Emma Garcia", "emma@timberwolves.com"),
    ]

    for reg_id, team_name, abbrev, league_id, season_id, division_id, org_name, org_email in registrations:
        try:
            conn.execute("""
                INSERT INTO team_registrations (
                    client_id, registration_id, team_name, abbreviation,
                    league_id, season_id, division_id,
                    organizer_name, organizer_email,
                    registered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (client_id, reg_id, team_name, abbrev, league_id, season_id, division_id, org_name, org_email, now))
            logger.info(f"Registered team: {team_name} ({abbrev}) in {division_id}")
            count += 1
        except sqlite3.IntegrityError:
            pass

    return count


def seed_rosters(conn: sqlite3.Connection, client_id: str = DEFAULT_CLIENT_ID) -> int:
    """Add players to team rosters.

    Creates realistic rosters with some players registered on multiple teams
    in different divisions with different jersey numbers.
    """
    now = int(time.time())
    count = 0

    # Get all registrations grouped by division
    registrations = conn.execute("""
        SELECT registration_id, division_id, team_name FROM team_registrations WHERE client_id = ?
    """, (client_id,)).fetchall()

    # Get all players with names
    players = conn.execute("""
        SELECT player_id, full_name FROM players WHERE client_id = ? ORDER BY player_id
    """, (client_id,)).fetchall()

    if not players or not registrations:
        return 0

    # Group registrations by division
    regs_by_division = {}
    reg_names = {}
    for reg in registrations:
        div_id = reg["division_id"]
        if div_id not in regs_by_division:
            regs_by_division[div_id] = []
        regs_by_division[div_id].append(reg["registration_id"])
        reg_names[reg["registration_id"]] = reg["team_name"]

    divisions = list(regs_by_division.keys())

    # Reserve first 10 players as "multi-team" players who play in different divisions
    multi_team_players = players[:min(10, len(players) // 4)]
    regular_players = players[len(multi_team_players):]
    multi_team_count = 0

    # Add multi-team players to teams in different divisions with different jersey numbers
    if len(divisions) >= 2:
        for i, player_row in enumerate(multi_team_players):
            player_id = player_row["player_id"]

            # Pick 2 different divisions for this player
            div1 = divisions[i % len(divisions)]
            div2 = divisions[(i + 1) % len(divisions)]

            # Pick a team from each division
            if regs_by_division[div1] and regs_by_division[div2]:
                reg1 = regs_by_division[div1][i % len(regs_by_division[div1])]
                reg2 = regs_by_division[div2][i % len(regs_by_division[div2])]

                # Different jersey numbers for each team
                jersey1 = (i % 15) + 1  # 1-15
                jersey2 = ((i + 7) % 15) + 1  # Different number

                position = random.choice(POSITIONS)

                try:
                    conn.execute("""
                        INSERT INTO roster_entries (client_id, registration_id, player_id, jersey_number, position, roster_status, added_at)
                        VALUES (?, ?, ?, ?, ?, 'active', ?)
                    """, (client_id, reg1, player_id, jersey1, position, now))
                    count += 1
                    multi_team_count += 1
                except sqlite3.IntegrityError:
                    pass

                try:
                    conn.execute("""
                        INSERT INTO roster_entries (client_id, registration_id, player_id, jersey_number, position, roster_status, added_at)
                        VALUES (?, ?, ?, ?, ?, 'active', ?)
                    """, (client_id, reg2, player_id, jersey2, position, now))
                    count += 1
                except sqlite3.IntegrityError:
                    pass

    # Distribute remaining regular players across teams (15 per team)
    players_per_team = max(1, len(regular_players) // len(registrations))
    player_idx = 0
    team_roster_counts = {}

    for reg in registrations:
        reg_id = reg["registration_id"]
        team_roster_counts[reg_id] = 0

        for jersey_num in range(1, min(players_per_team + 1, 16)):
            if player_idx >= len(regular_players):
                break

            player_id = regular_players[player_idx]["player_id"]
            position = random.choice(POSITIONS)

            try:
                conn.execute("""
                    INSERT INTO roster_entries (client_id, registration_id, player_id, jersey_number, position, roster_status, added_at)
                    VALUES (?, ?, ?, ?, ?, 'active', ?)
                """, (client_id, reg_id, player_id, jersey_num, position, now))
                count += 1
                team_roster_counts[reg_id] += 1
            except sqlite3.IntegrityError:
                pass

            player_idx += 1

    # Log summary
    if count > 0:
        for reg_id, roster_count in team_roster_counts.items():
            if roster_count > 0:
                logger.info(f"Added {roster_count} players to {reg_names.get(reg_id, reg_id)}")
        if multi_team_count > 0:
            logger.info(f"  ({multi_team_count} multi-team players playing in multiple divisions)")

    return count


def seed_games(conn: sqlite3.Connection, game_count: int = 8, client_id: str = DEFAULT_CLIENT_ID) -> int:
    """Create sample games for today and tomorrow."""
    now = int(time.time())
    count = 0

    # Get rinks and sheets
    sheets = conn.execute("""
        SELECT sheet_id, rink_id, name FROM rink_sheets WHERE client_id = ?
    """, (client_id,)).fetchall()
    if not sheets:
        return 0

    # Get registrations with team info for pairing
    regs = conn.execute("""
        SELECT registration_id, team_name, abbreviation
        FROM team_registrations
        WHERE client_id = ?
    """, (client_id,)).fetchall()
    if len(regs) < 2:
        return 0

    # Create games - distribute across days with unique matchups per day
    today = datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)

    # Track which teams are playing on each day to avoid conflicts
    teams_playing_today = set()
    teams_playing_tomorrow = set()

    # Create matchups by pairing teams
    games_created = 0
    reg_list = list(regs)

    for day_offset in range(2):  # Today and tomorrow
        teams_playing = teams_playing_today if day_offset == 0 else teams_playing_tomorrow
        game_date = today + timedelta(days=day_offset)
        hour_offset = 0

        # Create games for this day
        for i in range(0, len(reg_list) - 1, 2):
            if games_created >= game_count:
                break

            home_reg = reg_list[i]
            away_reg = reg_list[i + 1]

            # Skip if either team already playing today
            if home_reg["registration_id"] in teams_playing or away_reg["registration_id"] in teams_playing:
                continue

            # Mark teams as playing
            teams_playing.add(home_reg["registration_id"])
            teams_playing.add(away_reg["registration_id"])

            # Pick sheet (rotate through available sheets)
            sheet = sheets[games_created % len(sheets)]

            # Calculate game time (stagger by 2 hours)
            game_time = game_date + timedelta(hours=hour_offset)
            start_time = game_time.isoformat()
            hour_offset += 2

            # Create stable game_id based on date and teams
            date_str = game_time.strftime("%Y%m%d")
            game_id = f"game-{date_str}-{home_reg['abbreviation']}-{away_reg['abbreviation']}"

            try:
                conn.execute("""
                    INSERT INTO games (
                        client_id, game_id, rink_id, sheet_id, home_registration_id, away_registration_id,
                        home_team, away_team, home_abbrev, away_abbrev,
                        scheduled_start, start_time, period_length_min, num_periods, game_type, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 3, 'regular', ?)
                """, (
                    client_id,
                    game_id,
                    sheet["rink_id"],
                    sheet["sheet_id"],
                    home_reg["registration_id"],
                    away_reg["registration_id"],
                    home_reg["team_name"],
                    away_reg["team_name"],
                    home_reg["abbreviation"],
                    away_reg["abbreviation"],
                    start_time,
                    start_time,
                    20,  # period_length_min
                    now,
                ))
                time_str = game_time.strftime("%b %d %I:%M%p")
                logger.info(f"Created game: {home_reg['team_name']} vs {away_reg['team_name']} @ {sheet['name']} ({time_str})")
                games_created += 1
                count += 1
            except sqlite3.IntegrityError:
                # Game already exists (duplicate)
                pass

        # Rotate teams for next day to get different matchups
        reg_list = reg_list[1:] + reg_list[:1]

    return count


def clear_all(conn: sqlite3.Connection) -> dict:
    """Clear all seeded data (preserving rule_sets)."""
    tables = [
        "games",
        "roster_entries",
        "team_registrations",
        "league_season_divisions",
        "league_seasons",
        "players",
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


def seed_all(db_path: str, player_count: int = 120, game_count: int = 8, client_id: str = DEFAULT_CLIENT_ID) -> dict:
    """Seed all sample data in dependency order."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    results = {}

    # Seed client first
    results["client"] = seed_client(conn, client_id)

    # Seed all entities with client context
    results["leagues"] = seed_leagues(conn, client_id)
    results["seasons"] = seed_seasons(conn, client_id)
    results["divisions"] = seed_divisions(conn, client_id)
    results["rinks"] = seed_rinks(conn, client_id)
    results["players"] = seed_players(conn, player_count, client_id)
    results["league_seasons"] = seed_league_seasons(conn, client_id)
    results["league_season_divisions"] = seed_league_season_divisions(conn, client_id)
    results["registrations"] = seed_registrations(conn, client_id)
    results["rosters"] = seed_rosters(conn, client_id)
    results["games"] = seed_games(conn, game_count, client_id)

    conn.commit()
    conn.close()

    return results
