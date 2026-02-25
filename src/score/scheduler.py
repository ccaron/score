"""
Schedule generation library for Score.

Uses Google OR-Tools CP-SAT solver to generate fair hockey schedules.
"""

import math
import uuid
from dataclasses import dataclass
from datetime import date, time, datetime, timedelta
from pathlib import Path

import yaml
from ortools.sat.python import cp_model


# --- Data Classes ---

@dataclass
class Team:
    """A team in the schedule."""
    number: int  # Team number (1, 2, 3, ...)

    @property
    def team_id(self) -> str:
        """Unique identifier for the team."""
        return f"team-{self.number}"

    @property
    def name(self) -> str:
        """Display name for the team."""
        return f"Team {self.number}"

    @property
    def abbreviation(self) -> str:
        """Abbreviation for the team."""
        return f"T{self.number}"


@dataclass
class Sheet:
    """An ice sheet at a rink."""
    sheet_id: str
    name: str


@dataclass
class GameSlot:
    """A potential slot where a game could be scheduled."""
    slot_id: int
    week: int  # Week number (1-based)
    day: int   # Day of week (0=Monday, 6=Sunday)
    time: time
    sheet_id: str


@dataclass
class Matchup:
    """A potential game between two teams."""
    matchup_id: int
    home_team: Team
    away_team: Team
    division_id: str


@dataclass
class ScheduledGame:
    """A scheduled game in the abstract schedule."""
    game_id: str
    division_id: str
    home_team: str
    away_team: str
    home_abbrev: str
    away_abbrev: str
    sheet_id: str
    week: int  # Abstract week number
    day: int   # Day of week (0=Monday, 6=Sunday)
    time: time  # Time of day
    start_time: datetime | None  # Concrete datetime (None for abstract schedules)


@dataclass
class SolverSettings:
    """Settings for the constraint solver."""
    timeout_seconds: float = 60.0  # How long to search for better solutions
    # Constraint weights (higher = more important, 0 = disabled)
    weight_day: int = 5            # Balance games across days of week
    weight_time: int = 5           # Balance games across times of day
    weight_home_away: int = 20     # Balance home/away games
    weight_matchup: int = 5        # Spread games across opponents
    weight_consecutive_matchup: int = 50  # Penalize same opponent in back-to-back weeks
    weight_bye_distribution: int = 0     # Spread byes across first/second half of season
    # Hard constraints
    max_consecutive_byes: int = 1  # Max consecutive weeks without a game (0 = disabled)
    max_consecutive_game_slots: int = 0  # Max consecutive games at same time slot (0 = disabled)


@dataclass
class SlotDefinition:
    """Explicit (time, sheet) pair for a specific day."""
    time: time
    sheet_id: str


@dataclass
class DaySchedule:
    """Schedule configuration for a specific day of week."""
    day: int  # 0=Monday, 6=Sunday
    slots: list[SlotDefinition]


@dataclass
class ScheduleConfig:
    """Parsed configuration for schedule generation."""
    num_teams: int
    games_per_team: int
    sheets: list[Sheet]
    day_schedules: list[DaySchedule]
    solver: SolverSettings = None  # type: ignore

    def __post_init__(self):
        if self.solver is None:
            self.solver = SolverSettings()

    @property
    def teams(self) -> list[Team]:
        """Generate all teams."""
        return [Team(number=i+1) for i in range(self.num_teams)]


@dataclass
class SchedulerContext:
    """Pre-computed indexes for efficient constraint building."""
    slots_by_week: dict[int, list["GameSlot"]]
    slots_by_week_day: dict[tuple[int, int], list["GameSlot"]]  # (week, day) -> slots
    slots_by_time: dict[time, list["GameSlot"]]
    slots_by_sheet: dict[str, list["GameSlot"]]
    matchups_by_team: dict[str, list["Matchup"]]  # team_id -> matchups involving team
    matchups_by_pair: dict[tuple[str, str], list["Matchup"]]  # (team1, team2) -> matchups (ordered pair)
    sorted_weeks: list[int]


@dataclass
class FairnessReport:
    """Report on schedule fairness metrics."""
    game_slot_distribution: dict[str, dict[str, int]]  # team -> {game_slot (day+time) -> count}
    day_distribution: dict[str, dict[str, int]]  # team -> {day -> count}
    time_distribution: dict[str, dict[str, int]]  # team -> {time -> count}
    sheet_distribution: dict[str, dict[str, int]]  # team -> {sheet -> count}
    home_away_balance: dict[str, tuple[int, int]]  # team -> (home, away)
    opponent_distribution: dict[str, dict[str, int]]  # team -> {opponent -> count}
    bye_weeks: dict[str, int] | None = None  # team -> number of bye weeks
    bye_spread: dict[str, tuple[int, int]] | None = None  # team -> (first_half_byes, second_half_byes)
    consecutive_time_slots: dict[str, int] | None = None  # team -> max consecutive weeks at same time slot
    # Ice utilization
    total_slots: int = 0
    used_slots: int = 0
    total_game_days: int = 0  # Total available game days in season
    used_game_days: int = 0   # Days with at least one game
    games_by_date: dict[date, int] | None = None  # date -> number of games

    @property
    def unused_slots(self) -> int:
        """Number of unused ice slots."""
        return self.total_slots - self.used_slots

    @property
    def utilization_pct(self) -> float:
        """Ice utilization percentage."""
        if self.total_slots == 0:
            return 0.0
        return (self.used_slots / self.total_slots) * 100

    def summary(self) -> str:
        """Generate a human-readable summary."""
        lines = ["Fairness Report:", ""]

        # Ice utilization
        lines.append("  Ice Utilization:")
        lines.append(f"    Game days used: {self.used_game_days} of {self.total_game_days} available")
        lines.append(f"    Slots used: {self.used_slots} of {self.total_slots}")
        lines.append(f"    Unused slots: {self.unused_slots}")
        lines.append(f"    Utilization: {self.utilization_pct:.1f}%")
        lines.append("")

        # Game slot distribution (day + time)
        lines.append("  Game Slot Distribution:")
        if self.game_slot_distribution:
            first_team = list(self.game_slot_distribution.keys())[0]
            game_slots = list(self.game_slot_distribution[first_team].keys())
            header = "              " + "  ".join(f"{gs:>12}" for gs in game_slots)
            lines.append(header)

            for team, slots in self.game_slot_distribution.items():
                values = "  ".join(f"{slots.get(gs, 0):>12}" for gs in game_slots)
                lines.append(f"    {team:12} {values}")
        lines.append("")

        # Day distribution
        lines.append("  Day Distribution:")
        if self.day_distribution:
            first_team = list(self.day_distribution.keys())[0]
            days = list(self.day_distribution[first_team].keys())
            header = "              " + "  ".join(f"{d:>6}" for d in days)
            lines.append(header)

            for team, day_counts in self.day_distribution.items():
                values = "  ".join(f"{day_counts.get(d, 0):>6}" for d in days)
                lines.append(f"    {team:12} {values}")
        lines.append("")

        # Time distribution
        lines.append("  Time Distribution:")
        if self.time_distribution:
            first_team = list(self.time_distribution.keys())[0]
            times = list(self.time_distribution[first_team].keys())
            header = "              " + "  ".join(f"{t:>6}" for t in times)
            lines.append(header)

            for team, time_counts in self.time_distribution.items():
                values = "  ".join(f"{time_counts.get(t, 0):>6}" for t in times)
                lines.append(f"    {team:12} {values}")
        lines.append("")

        # Sheet distribution
        lines.append("  Sheet Distribution:")
        if self.sheet_distribution:
            first_team = list(self.sheet_distribution.keys())[0]
            sheets = list(self.sheet_distribution[first_team].keys())
            header = "              " + "  ".join(f"{s:>8}" for s in sheets)
            lines.append(header)

            for team, sheet_counts in self.sheet_distribution.items():
                values = "  ".join(f"{sheet_counts.get(s, 0):>8}" for s in sheets)
                lines.append(f"    {team:12} {values}")
        lines.append("")

        # Home/away balance
        lines.append("  Home/Away Balance:")
        for team, (home, away) in self.home_away_balance.items():
            lines.append(f"    {team:12} {home} home, {away} away")
        lines.append("")

        # Opponent distribution
        lines.append("  Opponent Distribution:")
        for team, opponents in self.opponent_distribution.items():
            # Extract just the team number for compact display (e.g., "Team 5" -> "T5")
            opp_str = ", ".join(
                f"T{opp.split()[-1]}({count})" for opp, count in opponents.items()
            )
            lines.append(f"    {team:12} {opp_str}")

        return "\n".join(lines)


# --- Day of Week Parsing ---

DAY_NAME_TO_INT = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

DAY_INT_TO_NAME = {
    0: "Mon",
    1: "Tue",
    2: "Wed",
    3: "Thu",
    4: "Fri",
    5: "Sat",
    6: "Sun",
}


def _parse_day_of_week(day_str: str) -> int:
    """Convert day name to integer (0=Monday, 6=Sunday)."""
    return DAY_NAME_TO_INT[day_str.lower()]


def _parse_time(time_str: str) -> time:
    """Parse time string 'HH:MM' to time object."""
    parts = time_str.split(":")
    return time(int(parts[0]), int(parts[1]))


# --- Config Loading ---

def load_config(path: Path) -> ScheduleConfig:
    """Load and validate schedule configuration from YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)

    # Parse top-level settings
    num_teams = data["num_teams"]
    games_per_team = data["games_per_team"]

    # Parse schedule settings - new day_schedules format
    schedule = data["schedule"]
    day_schedules = []
    all_sheet_ids = set()

    for day_sched in schedule["day_schedules"]:
        day_int = _parse_day_of_week(day_sched["day"])
        slot_defs = []
        for slot in day_sched["slots"]:
            time_obj = _parse_time(slot["time"])
            sheet_id = slot["sheet"]
            all_sheet_ids.add(sheet_id)
            slot_defs.append(SlotDefinition(time=time_obj, sheet_id=sheet_id))
        day_schedules.append(DaySchedule(day=day_int, slots=slot_defs))

    # Build Sheet objects from discovered sheet IDs
    sheets = [Sheet(sheet_id=sid, name=sid) for sid in sorted(all_sheet_ids)]

    # Parse solver settings (optional)
    solver_data = data.get("solver", {})
    solver = SolverSettings(
        timeout_seconds=solver_data.get("timeout_seconds", 60.0),
        weight_day=solver_data.get("weight_day", 5),
        weight_time=solver_data.get("weight_time", 5),
        weight_home_away=solver_data.get("weight_home_away", 20),
        weight_matchup=solver_data.get("weight_matchup", 5),
        weight_consecutive_matchup=solver_data.get("weight_consecutive_matchup", 50),
        weight_bye_distribution=solver_data.get("weight_bye_distribution", 0),
        max_consecutive_byes=solver_data.get("max_consecutive_byes", 1),
        max_consecutive_game_slots=solver_data.get("max_consecutive_game_slots", 0),
    )

    return ScheduleConfig(
        num_teams=num_teams,
        games_per_team=games_per_team,
        sheets=sheets,
        day_schedules=day_schedules,
        solver=solver,
    )


# --- Slot and Matchup Generation ---

def _generate_slots(config: ScheduleConfig) -> list[GameSlot]:
    """Generate all available game slots using abstract weeks."""

    # Calculate slots available per week
    slots_per_week = sum(len(ds.slots) for ds in config.day_schedules)

    if slots_per_week == 0:
        raise ValueError("No slots defined in day_schedules")

    # Calculate total games needed
    total_matchups = (config.num_teams * config.games_per_team) // 2

    # Use minimum weeks needed to fit all games (dense schedule)
    num_weeks = math.ceil(total_matchups / slots_per_week)

    # Generate slots for each week
    slots = []
    slot_id = 0

    for week in range(1, num_weeks + 1):
        for day_sched in config.day_schedules:
            for slot_def in day_sched.slots:
                slots.append(GameSlot(
                    slot_id=slot_id,
                    week=week,
                    day=day_sched.day,
                    time=slot_def.time,
                    sheet_id=slot_def.sheet_id,
                ))
                slot_id += 1

    return slots


def _generate_matchups(config: ScheduleConfig) -> list[Matchup]:
    """
    Generate all potential matchups (every team pair with home/away variants).

    Creates multiple copies of each matchup to allow repeated games between
    the same teams. The solver will select which matchups to actually schedule.

    We generate games_per_team copies for each directed pair (home vs away).
    This is the theoretical maximum any pair could need (if a team played
    only one opponent for all their games). This gives the solver maximum
    flexibility to satisfy all fairness constraints.
    """
    matchups = []
    matchup_id = 0

    teams = config.teams
    games_per_team = config.games_per_team

    for i, home_team in enumerate(teams):
        for j, away_team in enumerate(teams):
            if i != j:
                # Create games_per_team copies of this directed matchup
                # This is maximum flexibility - solver will use what it needs
                for _ in range(games_per_team):
                    matchups.append(Matchup(
                        matchup_id=matchup_id,
                        home_team=home_team,
                        away_team=away_team,
                        division_id="main",  # Single division
                    ))
                    matchup_id += 1

    return matchups


def _build_context(matchups: list[Matchup], slots: list[GameSlot]) -> SchedulerContext:
    """Build pre-computed indexes for efficient constraint building."""
    # Group slots by week
    slots_by_week: dict[int, list[GameSlot]] = {}
    for s in slots:
        if s.week not in slots_by_week:
            slots_by_week[s.week] = []
        slots_by_week[s.week].append(s)

    # Group slots by (week, day) tuple
    slots_by_week_day: dict[tuple[int, int], list[GameSlot]] = {}
    for s in slots:
        key = (s.week, s.day)
        if key not in slots_by_week_day:
            slots_by_week_day[key] = []
        slots_by_week_day[key].append(s)

    # Group slots by time
    slots_by_time: dict[time, list[GameSlot]] = {}
    for s in slots:
        if s.time not in slots_by_time:
            slots_by_time[s.time] = []
        slots_by_time[s.time].append(s)

    # Group slots by sheet
    slots_by_sheet: dict[str, list[GameSlot]] = {}
    for s in slots:
        if s.sheet_id not in slots_by_sheet:
            slots_by_sheet[s.sheet_id] = []
        slots_by_sheet[s.sheet_id].append(s)

    # Index matchups by team
    matchups_by_team: dict[str, list[Matchup]] = {}
    for m in matchups:
        home_id = m.home_team.team_id
        away_id = m.away_team.team_id
        if home_id not in matchups_by_team:
            matchups_by_team[home_id] = []
        if away_id not in matchups_by_team:
            matchups_by_team[away_id] = []
        matchups_by_team[home_id].append(m)
        matchups_by_team[away_id].append(m)

    # Index matchups by team pair (ordered: smaller ID first)
    matchups_by_pair: dict[tuple[str, str], list[Matchup]] = {}
    for m in matchups:
        t1, t2 = m.home_team.team_id, m.away_team.team_id
        pair_key = (min(t1, t2), max(t1, t2))
        if pair_key not in matchups_by_pair:
            matchups_by_pair[pair_key] = []
        matchups_by_pair[pair_key].append(m)

    sorted_weeks = sorted(slots_by_week.keys())

    return SchedulerContext(
        slots_by_week=slots_by_week,
        slots_by_week_day=slots_by_week_day,
        slots_by_time=slots_by_time,
        slots_by_sheet=slots_by_sheet,
        matchups_by_team=matchups_by_team,
        matchups_by_pair=matchups_by_pair,
        sorted_weeks=sorted_weeks,
    )


# --- Constraint Helpers ---

def _add_slot_constraints(model: cp_model.CpModel, x: dict, matchups: list[Matchup], slots: list[GameSlot]):
    """Each slot can have at most one game."""
    for s in slots:
        model.add_at_most_one(x[m.matchup_id, s.slot_id] for m in matchups)


def _add_matchup_constraints(model: cp_model.CpModel, x: dict, matchups: list[Matchup], slots: list[GameSlot]):
    """Each matchup can be scheduled at most once."""
    for m in matchups:
        model.add_at_most_one(x[m.matchup_id, s.slot_id] for s in slots)


def _add_symmetry_breaking(
    model: cp_model.CpModel,
    x: dict,
    slots: list[GameSlot],
    ctx: SchedulerContext,
):
    """
    Add symmetry breaking constraints to reduce equivalent solutions.

    For equivalent matchups (same directed pair, e.g., multiple copies of A vs B at home),
    we enforce that copies are used in order: copy 1 before copy 2 before copy 3, etc.
    This is done by requiring: if copy i+1 is scheduled, then copy i must also be scheduled.
    """
    # For each directed pair, get the matchups in order
    # Group by (home_team_id, away_team_id)
    directed_pairs: dict[tuple[str, str], list[int]] = {}
    for pair_key, pair_matchups in ctx.matchups_by_pair.items():
        for m in pair_matchups:
            directed_key = (m.home_team.team_id, m.away_team.team_id)
            if directed_key not in directed_pairs:
                directed_pairs[directed_key] = []
            directed_pairs[directed_key].append(m.matchup_id)

    # For each directed pair with multiple copies, enforce ordering
    for directed_key, matchup_ids in directed_pairs.items():
        if len(matchup_ids) <= 1:
            continue

        # Sort matchup IDs to ensure consistent ordering
        matchup_ids.sort()

        # Enforce: if matchup i+1 is used, matchup i must also be used
        # This ensures we always use copies in order (1, then 2, then 3, etc.)
        for i in range(len(matchup_ids) - 1):
            m1_id = matchup_ids[i]
            m2_id = matchup_ids[i + 1]

            # sum(x[m2, s]) <= sum(x[m1, s])
            # If m2 is scheduled (sum=1), m1 must also be scheduled (sum>=1)
            m1_scheduled = sum(x[m1_id, s.slot_id] for s in slots)
            m2_scheduled = sum(x[m2_id, s.slot_id] for s in slots)
            model.add(m2_scheduled <= m1_scheduled)


def _add_team_games_constraint(
    model: cp_model.CpModel,
    x: dict,
    slots: list[GameSlot],
    config: ScheduleConfig,
    ctx: SchedulerContext,
):
    """Each team plays exactly games_per_team games."""
    for t in config.teams:
        team_matchups = ctx.matchups_by_team.get(t.team_id, [])
        total_games = sum(
            x[m.matchup_id, s.slot_id]
            for m in team_matchups
            for s in slots
        )
        model.add(total_games == config.games_per_team)


def _add_one_game_per_team_per_day(
    model: cp_model.CpModel,
    x: dict,
    slots: list[GameSlot],
    config: ScheduleConfig,
    ctx: SchedulerContext,
):
    """Each team plays at most one game per (week, day) combination."""
    for t in config.teams:
        team_matchups = ctx.matchups_by_team.get(t.team_id, [])

        for _, week_day_slots in ctx.slots_by_week_day.items():
            # At most one game for this team on this (week, day)
            games_on_week_day = sum(
                x[m.matchup_id, s.slot_id]
                for m in team_matchups
                for s in week_day_slots
            )
            model.add(games_on_week_day <= 1)


def _add_max_consecutive_byes_constraint(
    model: cp_model.CpModel,
    x: dict,
    config: ScheduleConfig,
    ctx: SchedulerContext,
):
    """Ensure teams don't exceed max_consecutive_byes weeks without a game."""
    max_byes = config.solver.max_consecutive_byes

    # For each team, check each window of (max_byes + 1) consecutive weeks
    # At least one must have a game
    window_size = max_byes + 1

    for t in config.teams:
        team_matchups = ctx.matchups_by_team.get(t.team_id, [])

        for i in range(len(ctx.sorted_weeks) - window_size + 1):
            window_weeks = ctx.sorted_weeks[i:i + window_size]
            window_slots = []
            for w in window_weeks:
                window_slots.extend(ctx.slots_by_week[w])

            # Games for this team in this window
            games_in_window = sum(
                x[m.matchup_id, s.slot_id]
                for m in team_matchups
                for s in window_slots
            )

            # At least one game in this window of consecutive weeks
            model.add(games_in_window >= 1)


def _add_max_consecutive_time_slots_constraint(
    model: cp_model.CpModel,
    x: dict,
    config: ScheduleConfig,
    ctx: SchedulerContext,
):
    """
    Hard constraint: teams cannot play more than max_consecutive_game_slots
    consecutive games at the same time slot.

    For example, if max_consecutive_game_slots=2, a team cannot play 3 games
    in a row all at 18:00.
    """
    max_consec = config.solver.max_consecutive_game_slots
    window_size = max_consec + 1  # If max is 2, check windows of 3

    # Pre-compute: for each time slot, get slots grouped by week
    time_slots_by_week: dict[time, dict[int, list[GameSlot]]] = {}
    for ts, ts_slots in ctx.slots_by_time.items():
        time_slots_by_week[ts] = {}
        for s in ts_slots:
            if s.week not in time_slots_by_week[ts]:
                time_slots_by_week[ts][s.week] = []
            time_slots_by_week[ts][s.week].append(s)

    for t in config.teams:
        team_matchups = ctx.matchups_by_team.get(t.team_id, [])

        # For each time slot, check sliding windows across weeks
        for ts, slots_by_week in time_slots_by_week.items():
            # Check each window of consecutive weeks
            for i in range(len(ctx.sorted_weeks) - window_size + 1):
                window_weeks = ctx.sorted_weeks[i:i + window_size]

                # Get all slots at this time in this window
                window_slots = [s for w in window_weeks for s in slots_by_week.get(w, [])]

                if not window_slots:
                    continue

                # Count games at this time slot in this window
                games_at_time_in_window = sum(
                    x[m.matchup_id, s.slot_id]
                    for m in team_matchups
                    for s in window_slots
                )

                # Hard constraint: at most max_consec games at this time in this window
                model.add(games_at_time_in_window <= max_consec)


def _add_fairness_objective(
    model: cp_model.CpModel,
    x: dict,
    matchups: list[Matchup],
    slots: list[GameSlot],
    config: ScheduleConfig,
    ctx: SchedulerContext,
):
    """
    Minimize unfairness across days, times, home/away, and opponents.
    Weights from config.solver control relative importance of each constraint.
    """
    weights = config.solver

    # Separate penalty lists for each category
    home_away_penalties = []
    opponent_penalties = []

    # Calculate fairness penalties for all teams
    teams = config.teams
    games_per_team = config.games_per_team
    num_opponents = len(teams) - 1

    # Calculate tight bounds for deviation variables
    max_ha_deviation = (games_per_team + 1) // 2 + 1
    # For opponent deviation, we use scaled values: games * num_opponents
    # Maximum possible: games_per_team matchups * num_opponents
    max_opp_deviation = games_per_team if num_opponents > 0 else 0

    # --- Home/Away Balance ---
    expected_home = games_per_team // 2
    for t in teams:
        # Filter for home matchups only
        team_matchups = ctx.matchups_by_team.get(t.team_id, [])
        home_matchups = [m for m in team_matchups if m.home_team.team_id == t.team_id]
        home_games = sum(x[m.matchup_id, s.slot_id] for m in home_matchups for s in slots)

        imbalance = model.new_int_var(0, max_ha_deviation, f"ha_imbalance_{t.team_id}")
        model.add(imbalance >= home_games - expected_home)
        model.add(imbalance >= expected_home - home_games)
        home_away_penalties.append(imbalance)

    # --- Opponent Variety ---
    # Try to spread games across opponents evenly
    # Since CP-SAT requires integers, we scale the calculation:
    # Instead of minimizing |games - games_per_team/num_opponents|
    # We minimize |games * num_opponents - games_per_team|
    # This avoids fractional expected values while maintaining the same optimization goal
    # OPTIMIZATION: Only iterate upper triangle to avoid double-counting
    for i, t in enumerate(teams):
        for opp in teams[i+1:]:
            # Use pre-computed pair lookup (ordered pair key)
            pair_key = (min(t.team_id, opp.team_id),
                       max(t.team_id, opp.team_id))
            pair_matchups = ctx.matchups_by_pair.get(pair_key, [])

            games_vs_opp = sum(x[m.matchup_id, s.slot_id] for m in pair_matchups for s in slots)

            # Scaled deviation: |games * num_opponents - games_per_team|
            # This is equivalent to |games - games_per_team/num_opponents| but uses only integers
            scaled_games = games_vs_opp * num_opponents
            scaled_target = games_per_team

            deviation = model.new_int_var(0, max_opp_deviation * num_opponents, f"opp_dev_{t.team_id}_{opp.team_id}")
            model.add(deviation >= scaled_games - scaled_target)
            model.add(deviation >= scaled_target - scaled_games)
            opponent_penalties.append(deviation)

    # --- Consecutive Opponent Penalty ---
    # Penalize playing the same opponent in back-to-back weeks
    consecutive_opponent_penalties = []
    if weights.weight_consecutive_matchup > 0:
        sorted_weeks = ctx.sorted_weeks

        # For each pair of consecutive weeks
        for i in range(len(sorted_weeks) - 1):
            week1 = sorted_weeks[i]
            week2 = sorted_weeks[i + 1]
            slots_week1 = ctx.slots_by_week[week1]
            slots_week2 = ctx.slots_by_week[week2]

            # For each team pair (upper triangle only)
            for j, t1 in enumerate(config.teams):
                for t2 in config.teams[j+1:]:
                    pair_key = (min(t1.team_id, t2.team_id),
                               max(t1.team_id, t2.team_id))
                    pair_matchups = ctx.matchups_by_pair.get(pair_key, [])

                    games_week1 = sum(x[m.matchup_id, s.slot_id] for m in pair_matchups for s in slots_week1)
                    games_week2 = sum(x[m.matchup_id, s.slot_id] for m in pair_matchups for s in slots_week2)

                    # CORRECT formulation: consecutive = 1 iff both weeks have games
                    # games_week1, games_week2 ∈ {0,1} due to one-game-per-team-per-day constraint
                    consecutive = model.new_bool_var(f"consec_{t1.team_id}_{t2.team_id}_{i}")
                    # consecutive <= games_week1: if no game week1, consecutive=0
                    model.add(consecutive <= games_week1)
                    # consecutive <= games_week2: if no game week2, consecutive=0
                    model.add(consecutive <= games_week2)
                    # consecutive >= games_week1 + games_week2 - 1: if both have games, consecutive=1
                    model.add(consecutive >= games_week1 + games_week2 - 1)
                    consecutive_opponent_penalties.append(consecutive)

    # --- Bye Spread: Balance Byes Across First/Second Half ---
    bye_spread_penalties = []
    if weights.weight_bye_distribution > 0:
        sorted_weeks = ctx.sorted_weeks
        midpoint_idx = len(sorted_weeks) // 2
        first_half_weeks = set(sorted_weeks[:midpoint_idx])
        second_half_weeks = set(sorted_weeks[midpoint_idx:])

        first_half_slots = [s for s in slots if s.week in first_half_weeks]
        second_half_slots = [s for s in slots if s.week in second_half_weeks]

        # For each team, penalize imbalance between first and second half byes
        for t in config.teams:
            team_matchups = ctx.matchups_by_team.get(t.team_id, [])

            # Games in first half
            games_first_half = sum(
                x[m.matchup_id, s.slot_id]
                for m in team_matchups
                for s in first_half_slots
            )

            # Games in second half
            games_second_half = sum(
                x[m.matchup_id, s.slot_id]
                for m in team_matchups
                for s in second_half_slots
            )

            # Byes = available weeks - games played
            byes_first_half = len(first_half_weeks) - games_first_half
            byes_second_half = len(second_half_weeks) - games_second_half

            # Penalize difference in byes between halves
            bye_imbalance = model.new_int_var(0, len(sorted_weeks), f"bye_spread_{t.team_id}")
            model.add(bye_imbalance >= byes_first_half - byes_second_half)
            model.add(bye_imbalance >= byes_second_half - byes_first_half)
            bye_spread_penalties.append(bye_imbalance)

    # --- Day Balance: Balance games across days of week ---
    day_penalties = []
    if weights.weight_day > 0:
        # Get unique days from day_schedules
        unique_days = set(ds.day for ds in config.day_schedules)
        num_days = len(unique_days)
        expected_per_day = games_per_team // num_days
        max_day_deviation = games_per_team  # Max possible deviation

        for day in unique_days:
            # Get all slots on this day (across all weeks)
            day_slots = [s for s in slots if s.day == day]

            for t in teams:
                team_matchups = ctx.matchups_by_team.get(t.team_id, [])

                games_on_day = sum(
                    x[m.matchup_id, s.slot_id]
                    for m in team_matchups
                    for s in day_slots
                )

                # Deviation from expected
                day_name = DAY_INT_TO_NAME[day]
                deviation = model.new_int_var(0, max_day_deviation, f"day_dev_{day_name}_{t.team_id}")
                model.add(deviation >= games_on_day - expected_per_day)
                model.add(deviation >= expected_per_day - games_on_day)
                day_penalties.append(deviation)

    # --- Time Balance: Balance games across times of day ---
    time_penalties = []
    if weights.weight_time > 0:
        # Get unique times from day_schedules
        unique_times = set()
        for ds in config.day_schedules:
            for slot_def in ds.slots:
                unique_times.add(slot_def.time)
        num_times = len(unique_times)
        expected_per_time = games_per_team // num_times
        max_time_deviation = games_per_team  # Max possible deviation

        for time_val in unique_times:
            # Get all slots at this time (across all weeks and days)
            time_slots = [s for s in slots if s.time == time_val]

            for t in teams:
                team_matchups = ctx.matchups_by_team.get(t.team_id, [])

                games_at_time = sum(
                    x[m.matchup_id, s.slot_id]
                    for m in team_matchups
                    for s in time_slots
                )

                # Deviation from expected
                time_str = time_val.strftime("%H:%M")
                deviation = model.new_int_var(0, max_time_deviation, f"time_dev_{time_str}_{t.team_id}")
                model.add(deviation >= games_at_time - expected_per_time)
                model.add(deviation >= expected_per_time - games_at_time)
                time_penalties.append(deviation)

    # Combine all penalties with their respective weights
    total_objective = (
        weights.weight_day * sum(day_penalties) +
        weights.weight_time * sum(time_penalties) +
        weights.weight_home_away * sum(home_away_penalties) +
        weights.weight_matchup * sum(opponent_penalties) +
        weights.weight_consecutive_matchup * sum(consecutive_opponent_penalties) +
        weights.weight_bye_distribution * sum(bye_spread_penalties)
    )

    model.minimize(total_objective)


def _add_warmstart_hints(
    model: cp_model.CpModel,
    x: dict,
    matchups: list[Matchup],
    slots: list[GameSlot],
    config: ScheduleConfig,
    ctx: SchedulerContext,
):
    """
    Generate a greedy initial solution and provide it as hints to the solver.
    This can significantly speed up finding the first feasible solution.
    """
    # Track assignments: (matchup_id, slot_id) pairs that are selected
    assignments: set[tuple[int, int]] = set()
    used_slots: set[int] = set()
    used_matchups: set[int] = set()
    team_games_on_week_day: dict[str, set[tuple[int, int]]] = {t.team_id: set() for t in config.teams}
    team_game_count: dict[str, int] = {t.team_id: 0 for t in config.teams}

    # Get target games per team
    games_per_team = config.games_per_team

    # Sort slots by (week, day, time) for greedy assignment
    sorted_slots = sorted(slots, key=lambda s: (s.week, s.day, s.time, s.slot_id))

    # Group matchups by team pair for efficient lookup
    matchups_by_undirected_pair: dict[frozenset, list[Matchup]] = {}
    for m in matchups:
        pair = frozenset([m.home_team.team_id, m.away_team.team_id])
        if pair not in matchups_by_undirected_pair:
            matchups_by_undirected_pair[pair] = []
        matchups_by_undirected_pair[pair].append(m)

    # Greedy assignment: for each slot, try to assign a valid matchup
    for slot in sorted_slots:
        if slot.slot_id in used_slots:
            continue

        best_matchup = None
        best_score = float('-inf')

        # Find best matchup for this slot
        for pair, pair_matchups in matchups_by_undirected_pair.items():
            for m in pair_matchups:
                if m.matchup_id in used_matchups:
                    continue

                home_id = m.home_team.team_id
                away_id = m.away_team.team_id

                # Check constraints
                week_day = (slot.week, slot.day)
                if week_day in team_games_on_week_day[home_id] or week_day in team_games_on_week_day[away_id]:
                    continue
                if team_game_count[home_id] >= games_per_team or team_game_count[away_id] >= games_per_team:
                    continue

                # Score: prefer teams with fewer games
                score = -(team_game_count[home_id] + team_game_count[away_id])
                if score > best_score:
                    best_score = score
                    best_matchup = m

        if best_matchup:
            assignments.add((best_matchup.matchup_id, slot.slot_id))
            used_slots.add(slot.slot_id)
            used_matchups.add(best_matchup.matchup_id)
            home_id = best_matchup.home_team.team_id
            away_id = best_matchup.away_team.team_id
            week_day = (slot.week, slot.day)
            team_games_on_week_day[home_id].add(week_day)
            team_games_on_week_day[away_id].add(week_day)
            team_game_count[home_id] += 1
            team_game_count[away_id] += 1

    # Provide hints to solver
    for (m_id, s_id), var in x.items():
        if (m_id, s_id) in assignments:
            model.add_hint(var, 1)
        else:
            model.add_hint(var, 0)


# --- Solution Callback for Progress ---

class ScheduleProgressCallback(cp_model.CpSolverSolutionCallback):
    """Callback to show progress during solving."""

    def __init__(self, x: dict, matchups: list[Matchup], slots: list[GameSlot], config: ScheduleConfig, html_output: Path = None):
        super().__init__()
        self.solution_count = 0
        self.start_time = None
        self.stopped_early = False
        self.x = x
        self.matchups = matchups
        self.slots = slots
        self.config = config
        self.html_output = html_output

    def on_solution_callback(self):
        import time as time_module
        if self.start_time is None:
            self.start_time = time_module.time()

        self.solution_count += 1
        elapsed = time_module.time() - self.start_time
        obj = self.objective_value
        bound = self.best_objective_bound
        gap = 100 * (obj - bound) / obj if obj > 0 else 0

        print(f"  [{elapsed:5.1f}s] Solution #{self.solution_count}: objective={obj:.0f}, bound={bound:.0f}, gap={gap:.1f}%")

        # Generate HTML for this solution if output path provided
        if self.html_output:
            try:
                # Extract current solution
                games = self._extract_current_solution()

                # Generate fairness report
                report = analyze_fairness(games, self.config)

                # Write HTML (overwriting same file each time)
                _write_html_schedule(games, self.config, report, self.html_output)
                print(f"    → Updated {self.html_output}")
            except Exception as e:
                print(f"    Warning: Failed to generate HTML: {e}")

    def _extract_current_solution(self) -> list[ScheduledGame]:
        """Extract scheduled games from current solution."""
        games = []

        for m in self.matchups:
            for s in self.slots:
                if self.value(self.x[m.matchup_id, s.slot_id]):
                    # This matchup is scheduled in this slot
                    game_id = str(uuid.uuid4())[:8]

                    games.append(ScheduledGame(
                        game_id=game_id,
                        division_id=m.division_id,
                        home_team=m.home_team.name,
                        away_team=m.away_team.name,
                        home_abbrev=m.home_team.abbreviation,
                        away_abbrev=m.away_team.abbreviation,
                        sheet_id=s.sheet_id,
                        week=s.week,
                        day=s.day,
                        time=s.time,
                        start_time=None,  # Abstract schedule has no concrete datetime
                    ))

        # Sort by (week, day, time)
        games.sort(key=lambda g: (g.week, g.day, g.time))
        return games

    def stop(self):
        """Stop the search early."""
        self.stopped_early = True
        self.stop_search()


# --- Solution Extraction ---

def _extract_solution(
    solver: cp_model.CpSolver,
    x: dict,
    matchups: list[Matchup],
    slots: list[GameSlot],
    config: ScheduleConfig,
) -> list[ScheduledGame]:
    """Extract scheduled games from solver solution."""
    games = []

    for m in matchups:
        for s in slots:
            if solver.value(x[m.matchup_id, s.slot_id]):
                # This matchup is scheduled in this slot
                game_id = str(uuid.uuid4())[:8]

                games.append(ScheduledGame(
                    game_id=game_id,
                    division_id=m.division_id,
                    home_team=m.home_team.name,
                    away_team=m.away_team.name,
                    home_abbrev=m.home_team.abbreviation,
                    away_abbrev=m.away_team.abbreviation,
                    sheet_id=s.sheet_id,
                    week=s.week,
                    day=s.day,
                    time=s.time,
                    start_time=None,  # Abstract schedule has no concrete datetime
                ))

    # Sort by (week, day, time)
    games.sort(key=lambda g: (g.week, g.day, g.time))
    return games


# --- Main Functions ---

def generate_schedule(config: ScheduleConfig, html_output: Path = None) -> list[ScheduledGame]:
    """
    Generate a fair schedule using OR-Tools CP-SAT solver.

    Args:
        config: Schedule configuration
        html_output: Optional path to write intermediate HTML solutions

    Returns list of scheduled games, or raises if no solution found.
    """
    model = cp_model.CpModel()

    # 1. Generate all matchups and available slots
    matchups = _generate_matchups(config)
    slots = _generate_slots(config)

    # 1.5. Build pre-computed indexes for efficient constraint building
    ctx = _build_context(matchups, slots)

    # Calculate total games
    total_teams = len(config.teams)
    total_games = (config.num_teams * config.games_per_team) // 2

    print(f"Teams: {total_teams}")
    print(f"Games per team: {config.games_per_team}")
    print(f"Total games to schedule: {total_games}")
    print(f"Available slots: {len(slots)}")
    print(f"Potential matchups: {len(matchups)}")
    timeout_str = f"{config.solver.timeout_seconds}s" if config.solver.timeout_seconds > 0 else "none (infinite)"
    print(f"Solver timeout: {timeout_str}")
    print(f"Weights: day={config.solver.weight_day}, time={config.solver.weight_time}, "
          f"home_away={config.solver.weight_home_away}, matchup={config.solver.weight_matchup}, "
          f"consecutive_matchup={config.solver.weight_consecutive_matchup}, "
          f"bye_distribution={config.solver.weight_bye_distribution}")
    print(f"Hard constraints: max_consecutive_byes={config.solver.max_consecutive_byes}, "
          f"max_consecutive_game_slots={config.solver.max_consecutive_game_slots}")

    # 2. Create decision variables
    # x[m, s] = 1 if matchup m is assigned to slot s
    x = {}
    for m in matchups:
        for s in slots:
            x[m.matchup_id, s.slot_id] = model.new_bool_var(f"x_{m.matchup_id}_{s.slot_id}")

    # 3. Add constraints
    _add_slot_constraints(model, x, matchups, slots)
    _add_matchup_constraints(model, x, matchups, slots)
    _add_symmetry_breaking(model, x, slots, ctx)
    _add_team_games_constraint(model, x, slots, config, ctx)
    _add_one_game_per_team_per_day(model, x, slots, config, ctx)
    if config.solver.max_consecutive_byes > 0:
        _add_max_consecutive_byes_constraint(model, x, config, ctx)
    if config.solver.max_consecutive_game_slots > 0:
        _add_max_consecutive_time_slots_constraint(model, x, config, ctx)

    # 4. Add fairness objective
    _add_fairness_objective(model, x, matchups, slots, config, ctx)

    # 4.5. Generate greedy warmstart hints
    _add_warmstart_hints(model, x, matchups, slots, config, ctx)

    # 5. Solve
    solver = cp_model.CpSolver()

    # Performance optimizations
    import os
    num_workers = os.cpu_count() or 8
    solver.parameters.num_workers = num_workers
    solver.parameters.log_search_progress = False  # Reduce logging overhead
    solver.parameters.cp_model_presolve = True     # Enable presolve
    solver.parameters.linearization_level = 2      # Better LP relaxation

    if config.solver.timeout_seconds > 0:
        solver.parameters.max_time_in_seconds = config.solver.timeout_seconds
        print(f"\nSolving with {config.solver.timeout_seconds}s timeout, {num_workers} workers (Ctrl+C to stop early)...")
    else:
        print(f"\nSolving with no timeout, {num_workers} workers (Ctrl+C to stop and use best solution)...")
    callback = ScheduleProgressCallback(x, matchups, slots, config, html_output)

    # Handle Ctrl+C gracefully
    import signal
    original_handler = signal.getsignal(signal.SIGINT)

    def interrupt_handler(signum, frame):
        print("\n  Stopping early (keeping best solution)...")
        callback.stop()

    signal.signal(signal.SIGINT, interrupt_handler)
    try:
        status = solver.solve(model, callback)
    finally:
        signal.signal(signal.SIGINT, original_handler)

    status_name = solver.StatusName(status)
    print(f"\nOR-Tools CP-SAT solver: {status_name} solution found in {solver.WallTime():.2f}s")

    # Solver diagnostics
    print(f"  Branches explored: {solver.NumBranches():,}")
    print(f"  Conflicts: {solver.NumConflicts():,}")
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        obj_value = solver.ObjectiveValue()
        best_bound = solver.BestObjectiveBound()
        if obj_value > 0:
            gap_pct = 100 * (obj_value - best_bound) / obj_value
            print(f"  Objective value: {obj_value:.0f}")
            print(f"  Best possible: {best_bound:.0f}")
            print(f"  Optimality gap: {gap_pct:.1f}%")
            if gap_pct == 0:
                print("  (Solution is provably optimal)")
            else:
                print("  (Increase timeout for potentially better solution)")

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise ValueError(f"No feasible schedule found (status: {status_name})")

    # 6. Extract solution
    return _extract_solution(solver, x, matchups, slots, config)


def analyze_fairness(games: list[ScheduledGame], config: ScheduleConfig) -> FairnessReport:
    """Analyze fairness metrics for a generated schedule."""
    # Get unique game slots (day + time), days, and times from config
    game_slots_set = set()
    days_set = set()
    times_set = set()
    for day_sched in config.day_schedules:
        day_name = DAY_INT_TO_NAME[day_sched.day]
        days_set.add(day_name)
        for slot_def in day_sched.slots:
            time_str = slot_def.time.strftime("%H:%M")
            times_set.add(time_str)
            game_slot = f"{day_name} {time_str}"
            game_slots_set.add(game_slot)
    game_slots = sorted(game_slots_set)
    days = sorted(days_set)
    times = sorted(times_set)
    sheet_ids = [s.sheet_id for s in config.sheets]

    # Initialize structures
    game_slot_dist: dict[str, dict[str, int]] = {}
    day_dist: dict[str, dict[str, int]] = {}
    time_dist: dict[str, dict[str, int]] = {}
    sheet_dist: dict[str, dict[str, int]] = {}
    home_away: dict[str, tuple[int, int]] = {}
    opponent_dist: dict[str, dict[str, int]] = {}

    for t in config.teams:
        game_slot_dist[t.name] = {gs: 0 for gs in game_slots}
        day_dist[t.name] = {d: 0 for d in days}
        time_dist[t.name] = {tm: 0 for tm in times}
        sheet_dist[t.name] = {s: 0 for s in sheet_ids}
        home_away[t.name] = (0, 0)
        opponent_dist[t.name] = {}

    # Track games by week for utilization analysis
    games_by_week: dict[int, int] = {}

    # Count metrics
    for game in games:
        day_name = DAY_INT_TO_NAME[game.day]
        time_str = game.time.strftime("%H:%M")
        game_slot = f"{day_name} {time_str}"

        # Games by week
        games_by_week[game.week] = games_by_week.get(game.week, 0) + 1

        # Game slot counts (day + time)
        if game_slot in game_slot_dist[game.home_team]:
            game_slot_dist[game.home_team][game_slot] += 1
        if game_slot in game_slot_dist[game.away_team]:
            game_slot_dist[game.away_team][game_slot] += 1

        # Day counts
        if day_name in day_dist[game.home_team]:
            day_dist[game.home_team][day_name] += 1
        if day_name in day_dist[game.away_team]:
            day_dist[game.away_team][day_name] += 1

        # Time counts
        if time_str in time_dist[game.home_team]:
            time_dist[game.home_team][time_str] += 1
        if time_str in time_dist[game.away_team]:
            time_dist[game.away_team][time_str] += 1

        # Sheet counts
        if game.sheet_id in sheet_dist[game.home_team]:
            sheet_dist[game.home_team][game.sheet_id] += 1
        if game.sheet_id in sheet_dist[game.away_team]:
            sheet_dist[game.away_team][game.sheet_id] += 1

        # Home/away counts
        h, a = home_away[game.home_team]
        home_away[game.home_team] = (h + 1, a)
        h, a = home_away[game.away_team]
        home_away[game.away_team] = (h, a + 1)

        # Opponent counts
        if game.away_team not in opponent_dist[game.home_team]:
            opponent_dist[game.home_team][game.away_team] = 0
        opponent_dist[game.home_team][game.away_team] += 1

        if game.home_team not in opponent_dist[game.away_team]:
            opponent_dist[game.away_team][game.home_team] = 0
        opponent_dist[game.away_team][game.home_team] += 1

    # Calculate ice utilization (only up to last game week)
    slots = _generate_slots(config)
    last_game_week = max(game.week for game in games) if games else None
    if last_game_week:
        slots = [s for s in slots if s.week <= last_game_week]

    total_slots = len(slots)
    used_slots = len(games)

    # Count total game weeks (unique weeks in slots up to last game)
    total_game_days = len(set(s.week for s in slots))  # Really "weeks" not "days"
    used_game_days = len(games_by_week)

    # Calculate bye weeks for each team
    bye_weeks: dict[str, int] = {}
    game_weeks = set(s.week for s in slots)  # All available game weeks

    for team in config.teams:
        # Find all weeks where this team has a game
        team_game_weeks = set()
        for game in games:
            if game.home_team == team.name or game.away_team == team.name:
                team_game_weeks.add(game.week)

        # Bye weeks = available game weeks minus weeks where team played
        bye_weeks[team.name] = len(game_weeks - team_game_weeks)

    # Calculate bye spread (first half vs second half)
    bye_spread: dict[str, tuple[int, int]] = {}
    sorted_game_weeks = sorted(game_weeks)
    midpoint_idx = len(sorted_game_weeks) // 2
    first_half_weeks = set(sorted_game_weeks[:midpoint_idx])
    second_half_weeks = set(sorted_game_weeks[midpoint_idx:])

    for team in config.teams:
        # Find all weeks where this team has a game
        team_game_weeks = set()
        for game in games:
            if game.home_team == team.name or game.away_team == team.name:
                team_game_weeks.add(game.week)

        # Byes in first half = first half weeks - weeks team played in first half
        first_half_byes = len(first_half_weeks - team_game_weeks)
        # Byes in second half = second half weeks - weeks team played in second half
        second_half_byes = len(second_half_weeks - team_game_weeks)

        bye_spread[team.name] = (first_half_byes, second_half_byes)

    # Calculate max consecutive weeks at same time slot for each team
    # Only counts as consecutive if games are in consecutive weeks
    consecutive_time_slots: dict[str, int] = {}
    sorted_game_weeks_list = sorted(game_weeks)

    for team in config.teams:
        # Get this team's games sorted by (week, day, time)
        team_games = sorted(
            [g for g in games if g.home_team == team.name or g.away_team == team.name],
            key=lambda g: (g.week, g.day, g.time)
        )

        if len(team_games) < 2:
            consecutive_time_slots[team.name] = 1 if team_games else 0
            continue

        # Track consecutive runs at same time slot
        # Only count as consecutive if in consecutive weeks
        max_consecutive = 1
        current_consecutive = 1
        prev_time = team_games[0].time.strftime("%H:%M")
        prev_week = team_games[0].week

        for i in range(1, len(team_games)):
            curr_time = team_games[i].time.strftime("%H:%M")
            curr_week = team_games[i].week

            # Check if this is a consecutive week
            try:
                prev_idx = sorted_game_weeks_list.index(prev_week)
                curr_idx = sorted_game_weeks_list.index(curr_week)
                is_consecutive_week = (curr_idx == prev_idx + 1)
            except ValueError:
                is_consecutive_week = False

            if curr_time == prev_time and is_consecutive_week:
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 1

            prev_time = curr_time
            prev_week = curr_week

        consecutive_time_slots[team.name] = max_consecutive

    # Convert games_by_week to games_by_date for compatibility (using None as placeholder)
    games_by_date = None  # No longer meaningful for abstract schedules

    return FairnessReport(
        game_slot_distribution=game_slot_dist,
        day_distribution=day_dist,
        time_distribution=time_dist,
        sheet_distribution=sheet_dist,
        home_away_balance=home_away,
        opponent_distribution=opponent_dist,
        bye_weeks=bye_weeks,
        bye_spread=bye_spread,
        consecutive_time_slots=consecutive_time_slots,
        total_slots=total_slots,
        used_slots=used_slots,
        total_game_days=total_game_days,
        used_game_days=used_game_days,
        games_by_date=games_by_date,
    )


# --- CLI ---

def main():
    """Command-line interface for schedule generation."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m score.scheduler <config.yaml> [--html output.html]")
        sys.exit(1)

    config_path = Path(sys.argv[1])
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)

    # Check for HTML output flag
    html_output = None
    if "--html" in sys.argv:
        html_idx = sys.argv.index("--html")
        if html_idx + 1 < len(sys.argv):
            html_output = Path(sys.argv[html_idx + 1])
        else:
            print("Error: --html flag requires output filename")
            sys.exit(1)

    print(f"Loading config from: {config_path}")
    config = load_config(config_path)

    print(f"\nGenerating schedule with {config.num_teams} teams, {config.games_per_team} games each")

    games = generate_schedule(config, html_output)
    print(f"\nGenerated {len(games)} games")

    report = analyze_fairness(games, config)
    print(f"\n{report.summary()}")

    # Print full schedule with unused slots
    _print_full_schedule(games, config)

    # Generate HTML output if requested
    if html_output:
        _write_html_schedule(games, config, report, html_output)
        print(f"\nHTML schedule written to: {html_output}")
        print(f"Open in browser: file://{html_output.absolute()}")


def _write_html_schedule(games: list[ScheduledGame], config: ScheduleConfig, report: FairnessReport, output_path: Path):
    """Generate and write HTML schedule to file."""

    # Toggle to show/hide the full schedule rendering
    SHOW_SCHEDULE = True

    # Day names for display
    DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    # Group games by (week, day)
    games_by_week_day: dict[tuple[int, int], list[ScheduledGame]] = {}
    for game in games:
        key = (game.week, game.day)
        if key not in games_by_week_day:
            games_by_week_day[key] = []
        games_by_week_day[key].append(game)

    # Sort each week/day's games by time, then sheet
    for week_day_games in games_by_week_day.values():
        week_day_games.sort(key=lambda g: (g.time, g.sheet_id))

    sorted_week_days = sorted(games_by_week_day.keys())

    # Build HTML
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hockey Schedule - {config.num_teams} Teams</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f5f5f5;
            padding: 20px;
        }}

        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}

        h1 {{
            color: #2c3e50;
            margin-bottom: 10px;
            font-size: 2em;
        }}

        h2 {{
            color: #34495e;
            margin: 30px 0 15px 0;
            font-size: 1.5em;
            border-bottom: 2px solid #3498db;
            padding-bottom: 5px;
        }}

        details {{
            margin: 20px 0;
        }}

        details summary {{
            cursor: pointer;
            color: #34495e;
            font-size: 1.5em;
            font-weight: 600;
            border-bottom: 2px solid #3498db;
            padding-bottom: 5px;
            margin-bottom: 15px;
            list-style: none;
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        details summary::-webkit-details-marker {{
            display: none;
        }}

        details summary::before {{
            content: "▶";
            font-size: 0.7em;
            transition: transform 0.2s;
        }}

        details[open] summary::before {{
            transform: rotate(90deg);
        }}

        details summary:hover {{
            color: #3498db;
        }}

        h3 {{
            color: #34495e;
            margin: 20px 0 10px 0;
            font-size: 1.2em;
        }}

        .subtitle {{
            color: #7f8c8d;
            font-size: 1.1em;
            margin-bottom: 30px;
        }}

        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }}

        .stat-box {{
            background: #ecf0f1;
            padding: 15px;
            border-radius: 5px;
            text-align: center;
        }}

        .stat-value {{
            font-size: 2em;
            font-weight: bold;
            color: #2c3e50;
        }}

        .stat-label {{
            color: #7f8c8d;
            font-size: 0.9em;
            margin-top: 5px;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 15px 0;
        }}

        th, td {{
            padding: 10px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }}

        th {{
            background: #34495e;
            color: white;
            font-weight: 600;
        }}

        tr:hover {{
            background: #f8f9fa;
        }}

        .schedule-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }}

        .date-section {{
            margin: 0;
            page-break-inside: avoid;
            background: white;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            overflow: hidden;
        }}

        .date-header {{
            background: #3498db;
            color: white;
            padding: 12px 15px;
            font-weight: 600;
            font-size: 1em;
        }}

        .games-list {{
            padding: 10px 0;
        }}

        .bye-section {{
            padding: 10px 15px;
            background: #f8f9fa;
            border-top: 1px solid #e9ecef;
            font-size: 0.85em;
            color: #6c757d;
        }}

        .bye-label {{
            font-weight: 600;
            margin-bottom: 5px;
        }}

        .bye-teams {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }}

        .bye-team {{
            background: white;
            border: 1px solid #dee2e6;
            padding: 3px 8px;
            border-radius: 3px;
            font-size: 0.9em;
        }}

        .bye-team.consecutive {{
            background: #fff3cd;
            border-color: #ffc107;
            font-weight: 600;
        }}

        .game-line {{
            display: grid;
            grid-template-columns: 80px 100px 1fr;
            gap: 15px;
            align-items: center;
            padding: 10px 15px;
            border-bottom: 1px solid #f0f0f0;
            transition: background 0.2s;
        }}

        .game-line:last-child {{
            border-bottom: none;
        }}

        .game-line:hover {{
            background: #f8f9fa;
        }}

        .game-line.unused {{
            color: #999;
            font-style: italic;
        }}

        .game-time {{
            font-weight: 700;
            color: #2c3e50;
            font-size: 0.95em;
        }}

        .game-sheet {{
            color: #7f8c8d;
            font-size: 0.85em;
        }}

        .game-matchup {{
            font-weight: 600;
            font-size: 0.95em;
            color: #2c3e50;
        }}

        .game-line.unused .game-matchup {{
            color: #999;
        }}

        .games-grid {{
            display: none;
        }}

        .game-card {{
            display: none;
        }}

        .game-division {{
            display: none;
        }}

        .unused-slot {{
            background: #f8f9fa;
            border-left-color: #dee2e6;
            color: #6c757d;
            font-style: italic;
        }}

        .legend {{
            display: flex;
            flex-wrap: wrap;
            gap: 15px;
            margin: 20px 0;
            padding: 15px;
            background: #f8f9fa;
            border-radius: 5px;
        }}

        .legend-item {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .legend-color {{
            width: 30px;
            height: 20px;
            border-radius: 3px;
        }}

        .metrics-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin: 20px 0;
        }}

        .metric-card {{
            background: #f8f9fa;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}

        .metric-card h3 {{
            margin-top: 0;
            margin-bottom: 15px;
            color: #2c3e50;
            font-size: 1.1em;
            border-bottom: 2px solid #3498db;
            padding-bottom: 5px;
        }}

        .metric-card table {{
            margin: 0;
            font-size: 0.9em;
        }}

        .metric-card th {{
            background: #34495e;
            padding: 8px;
        }}

        .metric-card td {{
            padding: 8px;
        }}

        .bar-chart {{
            margin: 15px 0;
        }}

        .bar-row {{
            display: flex;
            align-items: center;
            margin-bottom: 8px;
            gap: 10px;
        }}

        .bar-label {{
            min-width: 60px;
            font-weight: 600;
            font-size: 0.9em;
        }}

        .bar-container {{
            flex: 1;
            display: flex;
            gap: 4px;
            align-items: center;
        }}

        .bar {{
            height: 24px;
            border-radius: 4px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.85em;
            font-weight: 600;
            color: white;
            transition: all 0.3s;
            min-width: 30px;
        }}

        .bar:hover {{
            opacity: 0.8;
            transform: scaleY(1.1);
        }}

        .bar-home {{
            background: #3498db;
        }}

        .bar-away {{
            background: #e74c3c;
        }}

        .bar-timeslot {{
            background: #2ecc71;
        }}

        .bar-balance {{
            margin-left: 10px;
            font-size: 0.9em;
            color: #7f8c8d;
        }}

        .matchup-matrix {{
            margin: 15px 0;
            overflow-x: auto;
        }}

        .matchup-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.8em;
        }}

        .matchup-table th,
        .matchup-table td {{
            padding: 6px;
            text-align: center;
            border: 1px solid #ddd;
        }}

        .matchup-table th {{
            background: #34495e;
            color: white;
            font-weight: 600;
        }}

        .matchup-table td {{
            background: #fff;
        }}

        .matchup-table .empty-cell {{
            background: #f8f9fa;
            color: #ccc;
        }}

        .matchup-table .team-label {{
            background: #ecf0f1;
            font-weight: 600;
            text-align: left;
        }}

        .matchup-perfect {{
            font-size: 1.2em;
            color: #27ae60;
            text-align: center;
            padding: 20px;
        }}

        .matchup-heatmap {{
            margin: 15px 0;
        }}

        .matchup-heatmap-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.8em;
        }}

        .matchup-heatmap-table th,
        .matchup-heatmap-table td {{
            padding: 8px;
            text-align: center;
            border: 1px solid #ddd;
        }}

        .matchup-heatmap-table th {{
            background: #34495e;
            color: white;
            font-weight: 600;
        }}

        .matchup-heatmap-table .team-label {{
            background: #ecf0f1;
            font-weight: 600;
        }}

        .matchup-heatmap-table .empty-cell {{
            background: #f8f9fa;
        }}

        .matchup-heatmap-table .matchup-perfect {{
            background: #d4edda;
            color: #155724;
            font-weight: 600;
        }}

        .matchup-heatmap-table .matchup-low {{
            background: #fff3cd;
            color: #856404;
            font-weight: 600;
        }}

        .matchup-heatmap-table .matchup-high {{
            background: #f8d7da;
            color: #721c24;
            font-weight: 600;
        }}

        @media print {{
            body {{
                background: white;
            }}
            .container {{
                box-shadow: none;
            }}
            .game-card:hover {{
                transform: none;
                box-shadow: none;
            }}
        }}

        @media (max-width: 768px) {{
            .schedule-grid {{
                grid-template-columns: 1fr;
            }}

            .game-line {{
                grid-template-columns: 70px 80px 1fr;
                gap: 10px;
            }}

            .stats {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Hockey Schedule</h1>
        <div class="subtitle">{config.num_teams} teams, {config.games_per_team} games each</div>

        <div class="stats">
            <div class="stat-box">
                <div class="stat-value">{len(games)}</div>
                <div class="stat-label">Total Games</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{len(sorted_week_days)}</div>
                <div class="stat-label">Game Days</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{config.num_teams}</div>
                <div class="stat-label">Teams</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{config.games_per_team}</div>
                <div class="stat-label">Games per Team</div>
            </div>
        </div>

        <details>
            <summary>Solver Settings</summary>
            <div class="stats">
            <div class="stat-box">
                <div class="stat-value">""" + (f"{config.solver.timeout_seconds:.0f}s" if config.solver.timeout_seconds > 0 else "∞") + """</div>
                <div class="stat-label">Timeout</div>
            </div>
        </div>

        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 30px;">
            <div style="background: #f8f9fa; padding: 20px; border-radius: 8px;">
                <h3 style="margin-top: 0; margin-bottom: 15px; color: #2c3e50; font-size: 1.1em; border-bottom: 2px solid #3498db; padding-bottom: 5px;">Soft Constraints (Weights)</h3>
                <table style="width: 100%; font-size: 0.9em;">
                    <tr><td style="padding: 5px 0;">Day Balance</td><td style="text-align: right; font-weight: 600;">""" + str(config.solver.weight_day) + """</td></tr>
                    <tr><td style="padding: 5px 0;">Time Balance</td><td style="text-align: right; font-weight: 600;">""" + str(config.solver.weight_time) + """</td></tr>
                    <tr><td style="padding: 5px 0;">Home/Away Balance</td><td style="text-align: right; font-weight: 600;">""" + str(config.solver.weight_home_away) + """</td></tr>
                    <tr><td style="padding: 5px 0;">Matchup Variety</td><td style="text-align: right; font-weight: 600;">""" + str(config.solver.weight_matchup) + """</td></tr>
                    <tr><td style="padding: 5px 0;">Consecutive Matchup Penalty</td><td style="text-align: right; font-weight: 600;">""" + str(config.solver.weight_consecutive_matchup) + """</td></tr>
                    <tr><td style="padding: 5px 0;">Bye Distribution</td><td style="text-align: right; font-weight: 600;">""" + str(config.solver.weight_bye_distribution) + """</td></tr>
                </table>
            </div>
            <div style="background: #f8f9fa; padding: 20px; border-radius: 8px;">
                <h3 style="margin-top: 0; margin-bottom: 15px; color: #2c3e50; font-size: 1.1em; border-bottom: 2px solid #e74c3c; padding-bottom: 5px;">Hard Constraints</h3>
                <table style="width: 100%; font-size: 0.9em;">
                    <tr><td style="padding: 5px 0;">Max Consecutive Byes</td><td style="text-align: right; font-weight: 600;">""" + (str(config.solver.max_consecutive_byes) if config.solver.max_consecutive_byes > 0 else "disabled") + """</td></tr>
                    <tr><td style="padding: 5px 0;">Max Consecutive Time Slots</td><td style="text-align: right; font-weight: 600;">""" + (str(config.solver.max_consecutive_game_slots) if config.solver.max_consecutive_game_slots > 0 else "disabled") + """</td></tr>
                </table>
            </div>
        </div>
        </details>

        <details open>
            <summary>Fairness Metrics</summary>
            <div class="metrics-grid">
            <div class="metric-card">
                <h3>Home/Away Balance</h3>
                <div style="margin-bottom: 10px; font-size: 0.85em;">
                    <span style="display: inline-block; width: 12px; height: 12px; background: #3498db; border-radius: 2px; margin-right: 4px;"></span>
                    <span style="margin-right: 12px;">Home</span>
                    <span style="display: inline-block; width: 12px; height: 12px; background: #e74c3c; border-radius: 2px; margin-right: 4px;"></span>
                    <span style="margin-right: 12px;">Away</span>
                </div>
                <div class="bar-chart">
"""

    # Calculate max value for scaling
    max_games = max(max(home, away) for home, away in report.home_away_balance.values())

    for team_name, (home, away) in sorted(report.home_away_balance.items()):
        # Calculate bar widths as percentages
        home_width = (home / max_games) * 100
        away_width = (away / max_games) * 100
        team_num = team_name.split()[-1]  # Extract number from "Team 1" -> "1"

        html += f"""                    <div class="bar-row">
                        <div class="bar-label">{team_num}</div>
                        <div class="bar-container">
                            <div class="bar bar-home" style="width: {home_width}%">{home}</div>
                            <div class="bar bar-away" style="width: {away_width}%">{away}</div>
                        </div>
                    </div>
"""

    html += """                </div>
            </div>

            <div class="metric-card">
                <h3>Day Distribution</h3>
"""

    # Get days
    if report.day_distribution:
        first_team = list(report.day_distribution.keys())[0]
        days = sorted(report.day_distribution[first_team].keys())

        # Add a legend showing days with different colors
        colors = ["#e74c3c", "#f39c12", "#f1c40f", "#2ecc71", "#3498db", "#9b59b6", "#e91e63"]
        html += """                <div style="margin-bottom: 10px; font-size: 0.85em;">
"""
        for i, day in enumerate(days):
            color = colors[i % len(colors)]
            html += f"""                    <span style="display: inline-block; width: 12px; height: 12px; background: {color}; border-radius: 2px; margin-right: 4px;"></span>
                    <span style="margin-right: 12px;">{day}</span>
"""
        html += """                </div>
"""

        # Calculate max value for scaling
        max_day_games = max(
            max(day_counts.values())
            for day_counts in report.day_distribution.values()
        )

        for team_name in sorted(report.day_distribution.keys()):
            day_counts = report.day_distribution[team_name]
            team_num = team_name.split()[-1]  # Extract number from "Team 1" -> "1"

            html += f"""                <div class="bar-row">
                    <div class="bar-label">{team_num}</div>
                    <div class="bar-container">
"""

            for i, day in enumerate(days):
                count = day_counts.get(day, 0)
                width = (count / max_day_games) * 100 if max_day_games > 0 else 0
                color = colors[i % len(colors)]
                html += f"""                        <div class="bar" style="width: {width}%; background: {color};" title="{day}">{count}</div>
"""

            html += """                    </div>
                </div>
"""

    html += """            </div>

            <div class="metric-card">
                <h3>Time Distribution</h3>
"""

    # Get times
    if report.time_distribution:
        first_team = list(report.time_distribution.keys())[0]
        times = sorted(report.time_distribution[first_team].keys())

        # Add a legend showing times with different shades
        colors = ["#2ecc71", "#27ae60", "#16a085", "#1abc9c"]
        html += """                <div style="margin-bottom: 10px; font-size: 0.85em;">
"""
        for i, tm in enumerate(times):
            color = colors[i % len(colors)]
            html += f"""                    <span style="display: inline-block; width: 12px; height: 12px; background: {color}; border-radius: 2px; margin-right: 4px;"></span>
                    <span style="margin-right: 12px;">{tm}</span>
"""
        html += """                </div>
"""

        # Calculate max value for scaling
        max_time_games = max(
            max(time_counts.values())
            for time_counts in report.time_distribution.values()
        )

        for team_name in sorted(report.time_distribution.keys()):
            time_counts = report.time_distribution[team_name]
            team_num = team_name.split()[-1]  # Extract number from "Team 1" -> "1"

            html += f"""                <div class="bar-row">
                    <div class="bar-label">{team_num}</div>
                    <div class="bar-container">
"""

            for i, tm in enumerate(times):
                count = time_counts.get(tm, 0)
                width = (count / max_time_games) * 100 if max_time_games > 0 else 0
                color = colors[i % len(colors)]
                html += f"""                        <div class="bar" style="width: {width}%; background: {color};" title="{tm}">{count}</div>
"""

            html += """                    </div>
                </div>
"""

    html += """            </div>

            <div class="metric-card">
                <h3>Ice Sheet Distribution</h3>
"""

    # Get sheets
    if report.sheet_distribution:
        first_team = list(report.sheet_distribution.keys())[0]
        sheets = sorted(report.sheet_distribution[first_team].keys())

        # Add a legend showing sheets with different colors (more contrasting)
        colors = ["#9b59b6", "#e67e22"]  # Purple and orange for better contrast
        html += """                <div style="margin-bottom: 10px; font-size: 0.85em;">
"""
        for i, sheet in enumerate(sheets):
            color = colors[i % len(colors)]
            html += f"""                    <span style="display: inline-block; width: 12px; height: 12px; background: {color}; border-radius: 2px; margin-right: 4px;"></span>
                    <span style="margin-right: 12px;">{sheet}</span>
"""
        html += """                </div>
"""

        # Calculate max value for scaling
        max_sheet_games = max(
            max(sheets_count.values())
            for sheets_count in report.sheet_distribution.values()
        )

        for team_name in sorted(report.sheet_distribution.keys()):
            sheet_counts = report.sheet_distribution[team_name]
            team_num = team_name.split()[-1]  # Extract number from "Team 1" -> "1"

            html += f"""                <div class="bar-row">
                    <div class="bar-label">{team_num}</div>
                    <div class="bar-container">
"""

            for i, sheet in enumerate(sheets):
                count = sheet_counts.get(sheet, 0)
                width = (count / max_sheet_games) * 100 if max_sheet_games > 0 else 0
                color = colors[i % len(colors)]
                html += f"""                        <div class="bar" style="width: {width}%; background: {color};" title="{sheet}">{count}</div>
"""

            html += """                    </div>
                </div>
"""

    html += """            </div>

            <div class="metric-card">
                <h3>Bye Spread</h3>
                <div style="margin-bottom: 10px; font-size: 0.85em;">
                    <span style="display: inline-block; width: 12px; height: 12px; background: #3498db; border-radius: 2px; margin-right: 4px;"></span>
                    <span style="margin-right: 12px;">1st Half</span>
                    <span style="display: inline-block; width: 12px; height: 12px; background: #e74c3c; border-radius: 2px; margin-right: 4px;"></span>
                    <span style="margin-right: 12px;">2nd Half</span>
                </div>
                <div class="bar-chart">
"""

    # Calculate max bye spread for scaling
    if report.bye_spread:
        max_bye_half = max(max(first, second) for first, second in report.bye_spread.values()) if report.bye_spread.values() else 0

        for team_name in sorted(report.bye_spread.keys()):
            first_half, second_half = report.bye_spread[team_name]
            first_width = (first_half / max_bye_half * 100) if max_bye_half > 0 else 0
            second_width = (second_half / max_bye_half * 100) if max_bye_half > 0 else 0
            team_num = team_name.split()[-1]  # Extract number from "Team 1" -> "1"

            html += f"""                    <div class="bar-row">
                        <div class="bar-label">{team_num}</div>
                        <div class="bar-container">
                            <div class="bar bar-home" style="width: {first_width}%">{first_half}</div>
                            <div class="bar bar-away" style="width: {second_width}%">{second_half}</div>
                        </div>
                    </div>
"""

    html += """                </div>
            </div>

            <div class="metric-card">
                <h3>Consecutive Time Slots</h3>
                <div class="bar-chart">
"""

    # Calculate max consecutive for scaling
    if report.consecutive_time_slots:
        max_consec = max(report.consecutive_time_slots.values()) if report.consecutive_time_slots.values() else 0

        for team_name in sorted(report.consecutive_time_slots.keys()):
            consec = report.consecutive_time_slots[team_name]
            width = (consec / max_consec * 100) if max_consec > 0 else 0
            team_num = team_name.split()[-1]  # Extract number from "Team 1" -> "1"
            # Color coding: green for 1-2, yellow for 3, red for 4+
            if consec <= 2:
                bar_color = "#27ae60"  # green
            elif consec == 3:
                bar_color = "#f39c12"  # orange/yellow
            else:
                bar_color = "#e74c3c"  # red

            html += f"""                    <div class="bar-row">
                        <div class="bar-label">{team_num}</div>
                        <div class="bar-container">
                            <div class="bar" style="width: {width}%; background: {bar_color};">{consec}</div>
                        </div>
                    </div>
"""

    html += """                </div>
            </div>

            <div class="metric-card">
                <h3>Team Matchups</h3>
"""

    # Get sorted team list
    teams = sorted(report.opponent_distribution.keys())

    # Calculate expected matchups per pair
    # Total games = num_teams * games_per_team / 2
    # Number of pairs = num_teams * (num_teams - 1) / 2
    # Expected per pair = games_per_team / (num_teams - 1)
    total_teams = len(teams)
    games_per_team = config.games_per_team
    if total_teams > 1:
        expected_matchups_float = games_per_team / (total_teams - 1)
        # Round to nearest integer for color coding
        expected_matchups = round(expected_matchups_float)

    # Always show the matrix with color coding
    html += """                <div class="matchup-heatmap">
                    <table class="matchup-heatmap-table">
                        <thead>
                            <tr>
                                <th></th>
"""
    for team in teams:
        team_num = team.split()[-1]  # Extract number from "Team 1" -> "1"
        html += f"                                <th>{team_num}</th>\n"

    html += """                            </tr>
                        </thead>
                        <tbody>
"""

    for i, team1 in enumerate(teams):
        team1_num = team1.split()[-1]  # Extract number from "Team 1" -> "1"
        html += "                            <tr>\n"
        html += f"                                <td class='team-label'>{team1_num}</td>\n"

        for j, team2 in enumerate(teams):
            if j < i:
                html += "                                <td class='empty-cell'>—</td>\n"
            elif j == i:
                html += "                                <td class='empty-cell'>—</td>\n"
            else:
                count = report.opponent_distribution.get(team1, {}).get(team2, 0)
                if count == expected_matchups:
                    cell_class = "matchup-perfect"
                elif count < expected_matchups:
                    cell_class = "matchup-low"
                else:
                    cell_class = "matchup-high"
                html += f"                                <td class='{cell_class}'>{count}</td>\n"

        html += "                            </tr>\n"

    html += """                        </tbody>
                    </table>
                </div>
"""

    html += """            </div>
        </div>
        </details>
"""

    # Schedule rendering (toggle with SHOW_SCHEDULE flag)
    if SHOW_SCHEDULE:
        html += """
        <details open>
            <summary>Schedule</summary>
            <div class="schedule-grid">
"""

        # Build a lookup of games by (week, day, time, sheet)
        game_lookup: dict[tuple[int, int, time, str], ScheduledGame] = {}
        for game in games:
            key = (game.week, game.day, game.time, game.sheet_id)
            game_lookup[key] = game

        # Generate all slots and group by (week, day)
        all_slots = _generate_slots(config)
        slots_by_week_day: dict[tuple[int, int], list[GameSlot]] = {}
        for s in all_slots:
            key = (s.week, s.day)
            if key not in slots_by_week_day:
                slots_by_week_day[key] = []
            slots_by_week_day[key].append(s)

        # Find the last week with any games scheduled
        last_game_week = max(game.week for game in games) if games else None

        # Only show weeks up to the last game
        if last_game_week:
            all_sorted_week_days = [(w, d) for (w, d) in sorted(slots_by_week_day.keys()) if w <= last_game_week]
        else:
            all_sorted_week_days = sorted(slots_by_week_day.keys())

        # Get all team names
        all_team_names = sorted([team.name for team in config.teams])

        # Track teams with byes from previous week
        previous_bye_teams = set()

        for week, day in all_sorted_week_days:
            week_day_slots = slots_by_week_day[(week, day)]
            # Sort slots by time, then sheet
            week_day_slots.sort(key=lambda s: (s.time, s.sheet_id))

            # Check if any games on this (week, day)
            games_on_week_day = [s for s in week_day_slots if (s.week, s.day, s.time, s.sheet_id) in game_lookup]

            if not games_on_week_day:
                # No games scheduled on this week/day - skip it entirely
                continue

            # Find which teams are playing on this week/day
            playing_teams = set()
            for slot in week_day_slots:
                key = (slot.week, slot.day, slot.time, slot.sheet_id)
                game = game_lookup.get(key)
                if game:
                    playing_teams.add(game.home_team)
                    playing_teams.add(game.away_team)

            # Teams with byes = all teams - playing teams
            bye_teams = sorted(set(all_team_names) - playing_teams)

            # Check which bye teams had byes last week (consecutive byes)
            consecutive_bye_teams = set(bye_teams) & previous_bye_teams

            html += f"""
        <div class="date-section">
            <div class="date-header">Week {week}, {DAY_NAMES[day]}</div>
            <div class="games-list">
"""

            for slot in week_day_slots:
                key = (slot.week, slot.day, slot.time, slot.sheet_id)
                game = game_lookup.get(key)

                if game:
                    html += f"""                <div class="game-line">
                    <div class="game-time">{game.time.strftime('%H:%M')}</div>
                    <div class="game-sheet">{game.sheet_id}</div>
                    <div class="game-matchup">{game.home_abbrev} vs {game.away_abbrev}</div>
                </div>
"""
                else:
                    html += f"""                <div class="game-line unused">
                    <div class="game-time">{slot.time.strftime('%H:%M')}</div>
                    <div class="game-sheet">{slot.sheet_id}</div>
                    <div class="game-matchup">UNUSED</div>
                </div>
"""

            html += """            </div>
"""

            # Add bye section if there are teams with byes
            if bye_teams:
                html += """            <div class="bye-section">
                <div class="bye-label">Byes:</div>
                <div class="bye-teams">
"""
                for team in bye_teams:
                    consecutive_class = " consecutive" if team in consecutive_bye_teams else ""
                    html += f"""                    <span class="bye-team{consecutive_class}">{team}</span>
"""
                html += """                </div>
            </div>
"""

            html += """        </div>
"""

            # Update previous_bye_teams for next iteration
            previous_bye_teams = set(bye_teams)

        html += """        </div>
        </details>
"""

    html += """    </div>
</body>
</html>
"""

    # Write to file
    with open(output_path, 'w') as f:
        f.write(html)


def _print_full_schedule(games: list[ScheduledGame], config: ScheduleConfig):
    """Print the full schedule showing all slots, with unused slots marked."""
    # Day names for display
    DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    # Build a lookup of games by (week, day, time, sheet)
    game_lookup: dict[tuple[int, int, time, str], ScheduledGame] = {}
    for game in games:
        key = (game.week, game.day, game.time, game.sheet_id)
        game_lookup[key] = game

    # Generate all slots and group by (week, day)
    slots = _generate_slots(config)
    slots_by_week_day: dict[tuple[int, int], list[GameSlot]] = {}
    for s in slots:
        key = (s.week, s.day)
        if key not in slots_by_week_day:
            slots_by_week_day[key] = []
        slots_by_week_day[key].append(s)

    # Sort by (week, day)
    sorted_week_days = sorted(slots_by_week_day.keys())

    # Find the last week with any games scheduled
    last_game_week = max(game.week for game in games) if games else None

    # Only show weeks up to the last game
    if last_game_week:
        sorted_week_days = [(w, d) for w, d in sorted_week_days if w <= last_game_week]

    print("\n" + "=" * 100)
    print("FULL SCHEDULE")
    print("=" * 100)

    for week, day in sorted_week_days:
        week_day_slots = slots_by_week_day[(week, day)]
        # Sort slots by time, then sheet
        week_day_slots.sort(key=lambda s: (s.time, s.sheet_id))

        # Check if any games on this (week, day)
        games_on_week_day = [s for s in week_day_slots if (s.week, s.day, s.time, s.sheet_id) in game_lookup]

        if not games_on_week_day:
            # No games scheduled on this week/day
            print(f"\nWeek {week}, {DAY_NAMES[day]}: NO GAMES SCHEDULED")
            continue

        print(f"\nWeek {week}, {DAY_NAMES[day]}:")

        for slot in week_day_slots:
            key = (slot.week, slot.day, slot.time, slot.sheet_id)
            game = game_lookup.get(key)

            time_str = slot.time.strftime("%H:%M")
            if game:
                print(f"  {time_str} | {slot.sheet_id:8} | [{game.division_id:10}] {game.home_abbrev} vs {game.away_abbrev}")
            else:
                print(f"  {time_str} | {slot.sheet_id:8} | --- UNUSED ---")

    print("\n" + "=" * 100)


if __name__ == "__main__":
    main()
