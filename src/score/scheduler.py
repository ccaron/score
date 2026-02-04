"""
Schedule generation library for Score.

Uses Google OR-Tools CP-SAT solver to generate fair hockey schedules.
"""

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
    registration_id: str
    name: str
    abbreviation: str
    division_id: str = ""  # Set when loaded as part of a division


@dataclass
class Sheet:
    """An ice sheet at a rink."""
    sheet_id: str
    name: str


@dataclass
class Division:
    """A division within the league."""
    division_id: str
    teams: list[Team]
    games_per_team: int


@dataclass
class GameSlot:
    """A potential slot where a game could be scheduled."""
    slot_id: int
    date: date
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
    """A scheduled game ready for database insertion."""
    game_id: str
    division_id: str
    home_registration_id: str
    away_registration_id: str
    home_team: str
    away_team: str
    home_abbrev: str
    away_abbrev: str
    sheet_id: str
    rink_id: str
    start_time: datetime
    period_length_min: int
    num_periods: int
    game_type: str


@dataclass
class SolverSettings:
    """Settings for the constraint solver."""
    timeout_seconds: float = 60.0  # How long to search for better solutions
    # Constraint weights (higher = more important, 0 = disabled)
    weight_time_slot: int = 10     # Balance games across time slots
    weight_sheet: int = 10         # Balance games across sheets
    weight_home_away: int = 20     # Balance home/away games
    weight_opponent: int = 5       # Spread games across opponents
    weight_packing: int = 1        # Pack games into earlier dates
    weight_no_consecutive_opponent: int = 50  # Penalize same opponent in back-to-back weeks
    # Hard constraints
    max_consecutive_byes: int = 1  # Max consecutive weeks without a game (0 = disabled)


@dataclass
class ScheduleConfig:
    """Parsed configuration for schedule generation."""
    league_id: str
    season_id: str
    rink_id: str
    sheets: list[Sheet]
    divisions: list[Division]
    period_length_min: int
    num_periods: int
    game_type: str
    days_of_week: list[int]  # 0=Monday, 6=Sunday
    start_date: date
    end_date: date
    blackout_dates: set[date]
    time_slots: list[time]
    solver: SolverSettings = None  # type: ignore

    def __post_init__(self):
        if self.solver is None:
            self.solver = SolverSettings()

    @property
    def all_teams(self) -> list[Team]:
        """Get all teams across all divisions."""
        teams = []
        for div in self.divisions:
            teams.extend(div.teams)
        return teams


@dataclass
class FairnessReport:
    """Report on schedule fairness metrics."""
    time_slot_distribution: dict[str, dict[str, int]]  # team -> {time_slot -> count}
    sheet_distribution: dict[str, dict[str, int]]  # team -> {sheet -> count}
    home_away_balance: dict[str, tuple[int, int]]  # team -> (home, away)
    opponent_distribution: dict[str, dict[str, int]]  # team -> {opponent -> count}
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

        # Time slot distribution
        lines.append("  Time Slot Distribution:")
        if self.time_slot_distribution:
            first_team = list(self.time_slot_distribution.keys())[0]
            time_slots = list(self.time_slot_distribution[first_team].keys())
            header = "              " + "  ".join(f"{ts:>6}" for ts in time_slots)
            lines.append(header)

            for team, slots in self.time_slot_distribution.items():
                values = "  ".join(f"{slots.get(ts, 0):>6}" for ts in time_slots)
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
            opp_str = ", ".join(f"{opp} ({count})" for opp, count in opponents.items())
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


def _parse_day_of_week(day_str: str) -> int:
    """Convert day name to integer (0=Monday, 6=Sunday)."""
    return DAY_NAME_TO_INT[day_str.lower()]


# --- Config Loading ---

def load_config(path: Path) -> ScheduleConfig:
    """Load and validate schedule configuration from YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)

    # Parse sheets
    sheets = [
        Sheet(sheet_id=s["sheet_id"], name=s["name"])
        for s in data["sheets"]
    ]

    # Parse divisions with their teams
    divisions = []
    for div_data in data["divisions"]:
        division_id = div_data["division_id"]
        teams = [
            Team(
                registration_id=t["registration_id"],
                name=t["name"],
                abbreviation=t["abbreviation"],
                division_id=division_id,
            )
            for t in div_data["teams"]
        ]
        divisions.append(Division(
            division_id=division_id,
            teams=teams,
            games_per_team=div_data["games_per_team"],
        ))

    # Parse schedule settings
    schedule = data["schedule"]
    days_of_week = [_parse_day_of_week(d) for d in schedule["days_of_week"]]
    start_date = datetime.strptime(schedule["start_date"], "%Y-%m-%d").date()
    end_date = datetime.strptime(schedule["end_date"], "%Y-%m-%d").date()

    blackout_dates = set()
    for d in schedule.get("blackout_dates", []):
        blackout_dates.add(datetime.strptime(d, "%Y-%m-%d").date())

    time_slots = []
    for t in schedule["time_slots"]:
        parts = t.split(":")
        time_slots.append(time(int(parts[0]), int(parts[1])))

    # Parse game settings
    game_settings = data["game_settings"]

    # Parse solver settings (optional)
    solver_data = data.get("solver", {})
    solver = SolverSettings(
        timeout_seconds=solver_data.get("timeout_seconds", 60.0),
        weight_time_slot=solver_data.get("weight_time_slot", 10),
        weight_sheet=solver_data.get("weight_sheet", 10),
        weight_home_away=solver_data.get("weight_home_away", 20),
        weight_opponent=solver_data.get("weight_opponent", 5),
        weight_packing=solver_data.get("weight_packing", 1),
        weight_no_consecutive_opponent=solver_data.get("weight_no_consecutive_opponent", 50),
        max_consecutive_byes=solver_data.get("max_consecutive_byes", 1),
    )

    return ScheduleConfig(
        league_id=data["league_id"],
        season_id=data["season_id"],
        rink_id=data["rink_id"],
        sheets=sheets,
        divisions=divisions,
        period_length_min=game_settings["period_length_min"],
        num_periods=game_settings["num_periods"],
        game_type=game_settings["game_type"],
        days_of_week=days_of_week,
        start_date=start_date,
        end_date=end_date,
        blackout_dates=blackout_dates,
        time_slots=time_slots,
        solver=solver,
    )


# --- Slot and Matchup Generation ---

def _generate_slots(config: ScheduleConfig) -> list[GameSlot]:
    """Generate all available game slots from config."""
    slots = []
    slot_id = 0

    current = config.start_date
    while current <= config.end_date:
        # Check if this day is allowed
        if current.weekday() in config.days_of_week and current not in config.blackout_dates:
            # Add a slot for each time and sheet combination
            for t in config.time_slots:
                for sheet in config.sheets:
                    slots.append(GameSlot(
                        slot_id=slot_id,
                        date=current,
                        time=t,
                        sheet_id=sheet.sheet_id,
                    ))
                    slot_id += 1
        current += timedelta(days=1)

    return slots


def _generate_matchups(config: ScheduleConfig) -> list[Matchup]:
    """
    Generate all potential matchups (every team pair with home/away variants).

    Creates multiple copies of each matchup to allow repeated games between
    the same teams. The solver will select which matchups to actually schedule.
    Only creates matchups within each division (no cross-division games).
    """
    matchups = []
    matchup_id = 0

    for division in config.divisions:
        teams = division.teams
        # Upper bound: all games could be against one opponent
        max_games_per_opponent = division.games_per_team

        for i, home_team in enumerate(teams):
            for j, away_team in enumerate(teams):
                if i != j:
                    # Create multiple copies of this matchup
                    for _ in range(max_games_per_opponent):
                        matchups.append(Matchup(
                            matchup_id=matchup_id,
                            home_team=home_team,
                            away_team=away_team,
                            division_id=division.division_id,
                        ))
                        matchup_id += 1

    return matchups


# --- Constraint Helpers ---

def _add_slot_constraints(model: cp_model.CpModel, x: dict, matchups: list[Matchup], slots: list[GameSlot]):
    """Each slot can have at most one game."""
    for s in slots:
        model.add_at_most_one(x[m.matchup_id, s.slot_id] for m in matchups)


def _add_matchup_constraints(model: cp_model.CpModel, x: dict, matchups: list[Matchup], slots: list[GameSlot]):
    """Each matchup can be scheduled at most once."""
    for m in matchups:
        model.add_at_most_one(x[m.matchup_id, s.slot_id] for s in slots)


def _add_team_games_constraint(
    model: cp_model.CpModel,
    x: dict,
    matchups: list[Matchup],
    slots: list[GameSlot],
    config: ScheduleConfig,
):
    """Each team plays exactly games_per_team games (per their division)."""
    for division in config.divisions:
        for t in division.teams:
            team_matchups = [m for m in matchups
                            if m.home_team.registration_id == t.registration_id
                            or m.away_team.registration_id == t.registration_id]
            total_games = sum(
                x[m.matchup_id, s.slot_id]
                for m in team_matchups
                for s in slots
            )
            model.add(total_games == division.games_per_team)


def _add_one_game_per_team_per_day(
    model: cp_model.CpModel,
    x: dict,
    matchups: list[Matchup],
    slots: list[GameSlot],
    config: ScheduleConfig,
):
    """Each team plays at most one game per day."""
    # Group slots by date
    slots_by_date: dict[date, list[GameSlot]] = {}
    for s in slots:
        if s.date not in slots_by_date:
            slots_by_date[s.date] = []
        slots_by_date[s.date].append(s)

    for t in config.all_teams:
        team_matchups = [m for m in matchups
                        if m.home_team.registration_id == t.registration_id
                        or m.away_team.registration_id == t.registration_id]

        for _, date_slots in slots_by_date.items():
            # At most one game for this team on this date
            games_on_date = sum(
                x[m.matchup_id, s.slot_id]
                for m in team_matchups
                for s in date_slots
            )
            model.add(games_on_date <= 1)


def _add_max_consecutive_byes_constraint(
    model: cp_model.CpModel,
    x: dict,
    matchups: list[Matchup],
    slots: list[GameSlot],
    config: ScheduleConfig,
):
    """Ensure teams don't exceed max_consecutive_byes weeks without a game."""
    max_byes = config.solver.max_consecutive_byes

    # Group slots by date
    slots_by_date: dict[date, list[GameSlot]] = {}
    for s in slots:
        if s.date not in slots_by_date:
            slots_by_date[s.date] = []
        slots_by_date[s.date].append(s)

    # Get sorted list of game dates
    sorted_dates = sorted(slots_by_date.keys())

    # For each team, check each window of (max_byes + 1) consecutive dates
    # At least one must have a game
    window_size = max_byes + 1

    for t in config.all_teams:
        team_matchups = [m for m in matchups
                        if m.home_team.registration_id == t.registration_id
                        or m.away_team.registration_id == t.registration_id]

        for i in range(len(sorted_dates) - window_size + 1):
            window_dates = sorted_dates[i:i + window_size]
            window_slots = []
            for d in window_dates:
                window_slots.extend(slots_by_date[d])

            # Games for this team in this window
            games_in_window = sum(
                x[m.matchup_id, s.slot_id]
                for m in team_matchups
                for s in window_slots
            )

            # At least one game in this window of consecutive weeks
            model.add(games_in_window >= 1)


def _add_fairness_objective(
    model: cp_model.CpModel,
    x: dict,
    matchups: list[Matchup],
    slots: list[GameSlot],
    config: ScheduleConfig,
):
    """
    Minimize unfairness across time slots, sheets, home/away, and opponents.
    Fairness is calculated per-division since each division may have different games_per_team.
    Weights from config.solver control relative importance of each constraint.
    """
    time_slots = config.time_slots
    sheets = config.sheets
    weights = config.solver

    # Separate penalty lists for each category
    time_slot_penalties = []
    sheet_penalties = []
    home_away_penalties = []
    opponent_penalties = []

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

    # Process each division separately for fairness
    for division in config.divisions:
        teams = division.teams
        games_per_team = division.games_per_team

        # --- Time Slot Balance ---
        # Each team should have roughly equal games at each time slot
        expected_per_time = games_per_team // len(time_slots)
        for ts in time_slots:
            time_slots_list = slots_by_time.get(ts, [])
            for t in teams:
                team_matchups = [m for m in matchups
                                if m.home_team.registration_id == t.registration_id
                                or m.away_team.registration_id == t.registration_id]

                games_at_time = sum(
                    x[m.matchup_id, s.slot_id]
                    for m in team_matchups
                    for s in time_slots_list
                )

                # Deviation from expected
                deviation = model.new_int_var(0, games_per_team, f"ts_dev_{ts}_{t.registration_id}")
                model.add(deviation >= games_at_time - expected_per_time)
                model.add(deviation >= expected_per_time - games_at_time)
                time_slot_penalties.append(deviation)

        # --- Sheet Balance ---
        expected_per_sheet = games_per_team // len(sheets)
        for sheet in sheets:
            sheet_slots = slots_by_sheet.get(sheet.sheet_id, [])
            for t in teams:
                team_matchups = [m for m in matchups
                                if m.home_team.registration_id == t.registration_id
                                or m.away_team.registration_id == t.registration_id]

                games_on_sheet = sum(
                    x[m.matchup_id, s.slot_id]
                    for m in team_matchups
                    for s in sheet_slots
                )

                deviation = model.new_int_var(0, games_per_team, f"sheet_dev_{sheet.sheet_id}_{t.registration_id}")
                model.add(deviation >= games_on_sheet - expected_per_sheet)
                model.add(deviation >= expected_per_sheet - games_on_sheet)
                sheet_penalties.append(deviation)

        # --- Home/Away Balance ---
        expected_home = games_per_team // 2
        for t in teams:
            home_matchups = [m for m in matchups if m.home_team.registration_id == t.registration_id]
            home_games = sum(x[m.matchup_id, s.slot_id] for m in home_matchups for s in slots)

            imbalance = model.new_int_var(0, games_per_team, f"ha_imbalance_{t.registration_id}")
            model.add(imbalance >= home_games - expected_home)
            model.add(imbalance >= expected_home - home_games)
            home_away_penalties.append(imbalance)

        # --- Opponent Variety ---
        # Try to spread games across opponents evenly (within division)
        num_opponents = len(teams) - 1
        expected_per_opponent = games_per_team // num_opponents if num_opponents > 0 else 0

        for t in teams:
            for opp in teams:
                if t.registration_id == opp.registration_id:
                    continue

                # Count games between t and opp (in either direction)
                pair_matchups = [m for m in matchups
                                if (m.home_team.registration_id == t.registration_id and
                                    m.away_team.registration_id == opp.registration_id) or
                                   (m.home_team.registration_id == opp.registration_id and
                                    m.away_team.registration_id == t.registration_id)]

                games_vs_opp = sum(x[m.matchup_id, s.slot_id] for m in pair_matchups for s in slots)

                deviation = model.new_int_var(0, games_per_team, f"opp_dev_{t.registration_id}_{opp.registration_id}")
                model.add(deviation >= games_vs_opp - expected_per_opponent)
                model.add(deviation >= expected_per_opponent - games_vs_opp)
                opponent_penalties.append(deviation)

    # --- Packing: Prefer Earlier Slots ---
    # Add a small penalty for each slot used, weighted by slot index
    # This encourages the solver to pack games into earlier dates
    packing_penalty = sum(
        x[m.matchup_id, s.slot_id] * s.slot_id
        for m in matchups
        for s in slots
    )

    # --- Consecutive Opponent Penalty ---
    # Penalize playing the same opponent in back-to-back weeks
    consecutive_opponent_penalties = []
    if weights.weight_no_consecutive_opponent > 0:
        # Group slots by date
        slots_by_date: dict[date, list[GameSlot]] = {}
        for s in slots:
            if s.date not in slots_by_date:
                slots_by_date[s.date] = []
            slots_by_date[s.date].append(s)

        sorted_dates = sorted(slots_by_date.keys())

        # For each pair of consecutive weeks
        for i in range(len(sorted_dates) - 1):
            date1 = sorted_dates[i]
            date2 = sorted_dates[i + 1]
            slots_week1 = slots_by_date[date1]
            slots_week2 = slots_by_date[date2]

            # For each division, check each pair of teams
            for division in config.divisions:
                teams = division.teams
                for t1 in teams:
                    for t2 in teams:
                        if t1.registration_id >= t2.registration_id:
                            continue

                        pair_matchups = [m for m in matchups
                                        if (m.home_team.registration_id == t1.registration_id and
                                            m.away_team.registration_id == t2.registration_id) or
                                           (m.home_team.registration_id == t2.registration_id and
                                            m.away_team.registration_id == t1.registration_id)]

                        games_week1 = sum(x[m.matchup_id, s.slot_id] for m in pair_matchups for s in slots_week1)
                        games_week2 = sum(x[m.matchup_id, s.slot_id] for m in pair_matchups for s in slots_week2)

                        # Create bool var for "has game in week 1"
                        has_game_w1 = model.new_bool_var(f"has_w1_{t1.registration_id}_{t2.registration_id}_{i}")
                        model.add(games_week1 >= 1).only_enforce_if(has_game_w1)
                        model.add(games_week1 == 0).only_enforce_if(has_game_w1.negated())

                        # Create bool var for "has game in week 2"
                        has_game_w2 = model.new_bool_var(f"has_w2_{t1.registration_id}_{t2.registration_id}_{i}")
                        model.add(games_week2 >= 1).only_enforce_if(has_game_w2)
                        model.add(games_week2 == 0).only_enforce_if(has_game_w2.negated())

                        # Penalty if both weeks have a game (has_game_w1 AND has_game_w2)
                        both_weeks = model.new_bool_var(f"consec_{t1.registration_id}_{t2.registration_id}_{i}")
                        model.add_bool_and([has_game_w1, has_game_w2]).only_enforce_if(both_weeks)
                        model.add_bool_or([has_game_w1.negated(), has_game_w2.negated()]).only_enforce_if(both_weeks.negated())
                        consecutive_opponent_penalties.append(both_weeks)

    # Combine all penalties with their respective weights
    total_objective = (
        weights.weight_time_slot * sum(time_slot_penalties) +
        weights.weight_sheet * sum(sheet_penalties) +
        weights.weight_home_away * sum(home_away_penalties) +
        weights.weight_opponent * sum(opponent_penalties) +
        weights.weight_packing * packing_penalty +
        weights.weight_no_consecutive_opponent * sum(consecutive_opponent_penalties)
    )

    model.minimize(total_objective)


# --- Solution Callback for Progress ---

class ScheduleProgressCallback(cp_model.CpSolverSolutionCallback):
    """Callback to show progress during solving."""

    def __init__(self):
        super().__init__()
        self.solution_count = 0
        self.start_time = None
        self.stopped_early = False

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
                start_time = datetime.combine(s.date, s.time)
                game_id = str(uuid.uuid4())[:8]

                games.append(ScheduledGame(
                    game_id=game_id,
                    division_id=m.division_id,
                    home_registration_id=m.home_team.registration_id,
                    away_registration_id=m.away_team.registration_id,
                    home_team=m.home_team.name,
                    away_team=m.away_team.name,
                    home_abbrev=m.home_team.abbreviation,
                    away_abbrev=m.away_team.abbreviation,
                    sheet_id=s.sheet_id,
                    rink_id=config.rink_id,
                    start_time=start_time,
                    period_length_min=config.period_length_min,
                    num_periods=config.num_periods,
                    game_type=config.game_type,
                ))

    # Sort by start time
    games.sort(key=lambda g: g.start_time)
    return games


# --- Main Functions ---

def generate_schedule(config: ScheduleConfig) -> list[ScheduledGame]:
    """
    Generate a fair schedule using OR-Tools CP-SAT solver.

    Returns list of scheduled games, or raises if no solution found.
    """
    model = cp_model.CpModel()

    # 1. Generate all matchups and available slots
    matchups = _generate_matchups(config)
    slots = _generate_slots(config)

    # Calculate total games across all divisions
    total_teams = len(config.all_teams)
    total_games = sum(len(d.teams) * d.games_per_team // 2 for d in config.divisions)

    print(f"Divisions: {len(config.divisions)}")
    print(f"Total teams: {total_teams}")
    for d in config.divisions:
        print(f"  {d.division_id}: {len(d.teams)} teams, {d.games_per_team} games each")
    print(f"Total games to schedule: {total_games}")
    print(f"Available slots: {len(slots)}")
    print(f"Potential matchups: {len(matchups)}")
    print(f"Solver timeout: {config.solver.timeout_seconds}s")
    print(f"Weights: time_slot={config.solver.weight_time_slot}, sheet={config.solver.weight_sheet}, "
          f"home_away={config.solver.weight_home_away}, opponent={config.solver.weight_opponent}, "
          f"packing={config.solver.weight_packing}")

    # 2. Create decision variables
    # x[m, s] = 1 if matchup m is assigned to slot s
    x = {}
    for m in matchups:
        for s in slots:
            x[m.matchup_id, s.slot_id] = model.new_bool_var(f"x_{m.matchup_id}_{s.slot_id}")

    # 3. Add constraints
    _add_slot_constraints(model, x, matchups, slots)
    _add_matchup_constraints(model, x, matchups, slots)
    _add_team_games_constraint(model, x, matchups, slots, config)
    _add_one_game_per_team_per_day(model, x, matchups, slots, config)
    if config.solver.max_consecutive_byes > 0:
        _add_max_consecutive_byes_constraint(model, x, matchups, slots, config)

    # 4. Add fairness objective
    _add_fairness_objective(model, x, matchups, slots, config)

    # 5. Solve
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = config.solver.timeout_seconds

    print("\nSolving (Ctrl+C to stop early and use best solution found)...")
    callback = ScheduleProgressCallback()

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
    time_slots = [t.strftime("%H:%M") for t in config.time_slots]
    sheet_ids = [s.sheet_id for s in config.sheets]

    # Initialize structures
    time_slot_dist: dict[str, dict[str, int]] = {}
    sheet_dist: dict[str, dict[str, int]] = {}
    home_away: dict[str, tuple[int, int]] = {}
    opponent_dist: dict[str, dict[str, int]] = {}

    for t in config.all_teams:
        time_slot_dist[t.name] = {ts: 0 for ts in time_slots}
        sheet_dist[t.name] = {s: 0 for s in sheet_ids}
        home_away[t.name] = (0, 0)
        opponent_dist[t.name] = {}

    # Track games by date for utilization analysis
    games_by_date: dict[date, int] = {}

    # Count metrics
    for game in games:
        time_str = game.start_time.strftime("%H:%M")
        game_date = game.start_time.date()

        # Games by date
        games_by_date[game_date] = games_by_date.get(game_date, 0) + 1

        # Time slot counts
        if time_str in time_slot_dist[game.home_team]:
            time_slot_dist[game.home_team][time_str] += 1
        if time_str in time_slot_dist[game.away_team]:
            time_slot_dist[game.away_team][time_str] += 1

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

    # Calculate ice utilization (only up to last game date)
    slots = _generate_slots(config)
    last_game_date = max(game.start_time.date() for game in games) if games else None
    if last_game_date:
        slots = [s for s in slots if s.date <= last_game_date]

    total_slots = len(slots)
    used_slots = len(games)

    # Count total game days (unique dates in slots up to last game)
    total_game_days = len(set(s.date for s in slots))
    used_game_days = len(games_by_date)

    return FairnessReport(
        time_slot_distribution=time_slot_dist,
        sheet_distribution=sheet_dist,
        home_away_balance=home_away,
        opponent_distribution=opponent_dist,
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
        print("Usage: python -m score.scheduler <config.yaml>")
        sys.exit(1)

    config_path = Path(sys.argv[1])
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)

    print(f"Loading config from: {config_path}")
    config = load_config(config_path)

    div_names = ", ".join(d.division_id for d in config.divisions)
    print(f"\nGenerating schedule for: {config.league_id} - {config.season_id}")
    print(f"Divisions: {div_names}")

    games = generate_schedule(config)
    print(f"\nGenerated {len(games)} games")

    report = analyze_fairness(games, config)
    print(f"\n{report.summary()}")

    # Print full schedule with unused slots
    _print_full_schedule(games, config)


def _print_full_schedule(games: list[ScheduledGame], config: ScheduleConfig):
    """Print the full schedule showing all slots, with unused slots marked."""
    # Build a lookup of games by (date, time, sheet)
    game_lookup: dict[tuple[date, time, str], ScheduledGame] = {}
    for game in games:
        key = (game.start_time.date(), game.start_time.time(), game.sheet_id)
        game_lookup[key] = game

    # Generate all slots and group by date
    slots = _generate_slots(config)
    slots_by_date: dict[date, list[GameSlot]] = {}
    for s in slots:
        if s.date not in slots_by_date:
            slots_by_date[s.date] = []
        slots_by_date[s.date].append(s)

    # Sort dates
    sorted_dates = sorted(slots_by_date.keys())

    # Find the last date with any games scheduled
    last_game_date = max(game.start_time.date() for game in games) if games else None

    # Only show dates up to the last game
    if last_game_date:
        sorted_dates = [d for d in sorted_dates if d <= last_game_date]

    print("\n" + "=" * 100)
    print("FULL SCHEDULE")
    print("=" * 100)

    for game_date in sorted_dates:
        date_slots = slots_by_date[game_date]
        # Sort slots by time, then sheet
        date_slots.sort(key=lambda s: (s.time, s.sheet_id))

        # Check if any games on this date
        games_on_date = [s for s in date_slots if (s.date, s.time, s.sheet_id) in game_lookup]

        if not games_on_date:
            # No games scheduled on this date
            print(f"\n{game_date.strftime('%Y-%m-%d (%A)')}: NO GAMES SCHEDULED")
            continue

        print(f"\n{game_date.strftime('%Y-%m-%d (%A)')}:")

        for slot in date_slots:
            key = (slot.date, slot.time, slot.sheet_id)
            game = game_lookup.get(key)

            time_str = slot.time.strftime("%H:%M")
            if game:
                print(f"  {time_str} | {slot.sheet_id:8} | [{game.division_id:10}] {game.home_abbrev} vs {game.away_abbrev}")
            else:
                print(f"  {time_str} | {slot.sheet_id:8} | --- UNUSED ---")

    print("\n" + "=" * 100)


if __name__ == "__main__":
    main()
