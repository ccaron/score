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
    weight_bye_spread: int = 0     # Spread byes across first/second half of season
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
    bye_weeks: dict[str, int] | None = None  # team -> number of bye weeks
    bye_spread: dict[str, tuple[int, int]] | None = None  # team -> (first_half_byes, second_half_byes)
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
        weight_bye_spread=solver_data.get("weight_bye_spread", 0),
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

    # --- Bye Spread: Balance Byes Across First/Second Half ---
    bye_spread_penalties = []
    if weights.weight_bye_spread > 0:
        # Group slots by date to find midpoint
        slots_by_date: dict[date, list[GameSlot]] = {}
        for s in slots:
            if s.date not in slots_by_date:
                slots_by_date[s.date] = []
            slots_by_date[s.date].append(s)

        sorted_dates = sorted(slots_by_date.keys())
        midpoint_idx = len(sorted_dates) // 2
        first_half_dates = sorted_dates[:midpoint_idx]
        second_half_dates = sorted_dates[midpoint_idx:]

        first_half_slots = [s for s in slots if s.date in first_half_dates]
        second_half_slots = [s for s in slots if s.date in second_half_dates]

        # For each team, penalize imbalance between first and second half byes
        for t in config.all_teams:
            team_matchups = [m for m in matchups
                            if m.home_team.registration_id == t.registration_id
                            or m.away_team.registration_id == t.registration_id]

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

            # Byes = available dates - games played
            byes_first_half = len(first_half_dates) - games_first_half
            byes_second_half = len(second_half_dates) - games_second_half

            # Penalize difference in byes between halves
            bye_imbalance = model.new_int_var(0, len(sorted_dates), f"bye_spread_{t.registration_id}")
            model.add(bye_imbalance >= byes_first_half - byes_second_half)
            model.add(bye_imbalance >= byes_second_half - byes_first_half)
            bye_spread_penalties.append(bye_imbalance)

    # Combine all penalties with their respective weights
    total_objective = (
        weights.weight_time_slot * sum(time_slot_penalties) +
        weights.weight_sheet * sum(sheet_penalties) +
        weights.weight_home_away * sum(home_away_penalties) +
        weights.weight_opponent * sum(opponent_penalties) +
        weights.weight_packing * packing_penalty +
        weights.weight_no_consecutive_opponent * sum(consecutive_opponent_penalties) +
        weights.weight_bye_spread * sum(bye_spread_penalties)
    )

    model.minimize(total_objective)


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
                        rink_id=self.config.rink_id,
                        start_time=start_time,
                        period_length_min=self.config.period_length_min,
                        num_periods=self.config.num_periods,
                        game_type=self.config.game_type,
                    ))

        # Sort by start time
        games.sort(key=lambda g: g.start_time)
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
    timeout_str = f"{config.solver.timeout_seconds}s" if config.solver.timeout_seconds > 0 else "none (infinite)"
    print(f"Solver timeout: {timeout_str}")
    print(f"Weights: time_slot={config.solver.weight_time_slot}, sheet={config.solver.weight_sheet}, "
          f"home_away={config.solver.weight_home_away}, opponent={config.solver.weight_opponent}, "
          f"packing={config.solver.weight_packing}, no_consecutive_opponent={config.solver.weight_no_consecutive_opponent}, "
          f"bye_spread={config.solver.weight_bye_spread}")
    print(f"Hard constraints: max_consecutive_byes={config.solver.max_consecutive_byes}")

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
    if config.solver.timeout_seconds > 0:
        solver.parameters.max_time_in_seconds = config.solver.timeout_seconds
        print(f"\nSolving with {config.solver.timeout_seconds}s timeout (Ctrl+C to stop early and use best solution found)...")
    else:
        print("\nSolving with no timeout (Ctrl+C to stop and use best solution found)...")
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

    # Calculate bye weeks for each team
    bye_weeks: dict[str, int] = {}
    game_dates = set(s.date for s in slots)  # All available game dates

    for team in config.all_teams:
        # Find all dates where this team has a game
        team_game_dates = set()
        for game in games:
            if game.home_team == team.name or game.away_team == team.name:
                team_game_dates.add(game.start_time.date())

        # Bye weeks = available game dates minus dates where team played
        bye_weeks[team.name] = len(game_dates - team_game_dates)

    # Calculate bye spread (first half vs second half)
    bye_spread: dict[str, tuple[int, int]] = {}
    sorted_game_dates = sorted(game_dates)
    midpoint_idx = len(sorted_game_dates) // 2
    first_half_dates = set(sorted_game_dates[:midpoint_idx])
    second_half_dates = set(sorted_game_dates[midpoint_idx:])

    for team in config.all_teams:
        # Find all dates where this team has a game
        team_game_dates = set()
        for game in games:
            if game.home_team == team.name or game.away_team == team.name:
                team_game_dates.add(game.start_time.date())

        # Byes in first half = first half dates - dates team played in first half
        first_half_byes = len(first_half_dates - team_game_dates)
        # Byes in second half = second half dates - dates team played in second half
        second_half_byes = len(second_half_dates - team_game_dates)

        bye_spread[team.name] = (first_half_byes, second_half_byes)

    return FairnessReport(
        time_slot_distribution=time_slot_dist,
        sheet_distribution=sheet_dist,
        home_away_balance=home_away,
        opponent_distribution=opponent_dist,
        bye_weeks=bye_weeks,
        bye_spread=bye_spread,
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

    div_names = ", ".join(d.division_id for d in config.divisions)
    print(f"\nGenerating schedule for: {config.league_id} - {config.season_id}")
    print(f"Divisions: {div_names}")

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

    # Group games by date
    games_by_date: dict[date, list[ScheduledGame]] = {}
    for game in games:
        game_date = game.start_time.date()
        if game_date not in games_by_date:
            games_by_date[game_date] = []
        games_by_date[game_date].append(game)

    # Sort each day's games by time, then sheet
    for date_games in games_by_date.values():
        date_games.sort(key=lambda g: (g.start_time.time(), g.sheet_id))

    sorted_dates = sorted(games_by_date.keys())

    # Assign colors to divisions
    division_colors = {
        div.division_id: f"hsl({i * 137.5 % 360}, 65%, 85%)"
        for i, div in enumerate(config.divisions)
    }

    # Build HTML
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Schedule: {config.league_id} - {config.season_id}</title>
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
        <div class="subtitle">{config.league_id} - {config.season_id}</div>

        <div class="stats">
            <div class="stat-box">
                <div class="stat-value">{len(games)}</div>
                <div class="stat-label">Total Games</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{len(sorted_dates)}</div>
                <div class="stat-label">Game Days</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{report.utilization_pct:.1f}%</div>
                <div class="stat-label">Ice Utilization</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{len(config.all_teams)}</div>
                <div class="stat-label">Teams</div>
            </div>
        </div>

        <h2>Division Legend</h2>
        <div class="legend">
"""

    for div in config.divisions:
        html += f"""            <div class="legend-item">
                <div class="legend-color" style="background: {division_colors[div.division_id]}"></div>
                <span>{div.division_id} ({len(div.teams)} teams, {div.games_per_team} games each)</span>
            </div>
"""

    html += """        </div>

        <h2>Fairness Metrics</h2>
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

        html += f"""                    <div class="bar-row">
                        <div class="bar-label">{team_name}</div>
                        <div class="bar-container">
                            <div class="bar bar-home" style="width: {home_width}%">{home}</div>
                            <div class="bar bar-away" style="width: {away_width}%">{away}</div>
                        </div>
                    </div>
"""

    html += """                </div>
            </div>

            <div class="metric-card">
                <h3>Time Slot Distribution</h3>
"""

    # Get time slots
    if report.time_slot_distribution:
        first_team = list(report.time_slot_distribution.keys())[0]
        time_slots = sorted(report.time_slot_distribution[first_team].keys())

        # Add a legend showing time slots with different shades
        colors = ["#2ecc71", "#27ae60", "#16a085", "#1abc9c"]
        html += """                <div style="margin-bottom: 10px; font-size: 0.85em;">
"""
        for i, ts in enumerate(time_slots):
            color = colors[i % len(colors)]
            html += f"""                    <span style="display: inline-block; width: 12px; height: 12px; background: {color}; border-radius: 2px; margin-right: 4px;"></span>
                    <span style="margin-right: 12px;">{ts}</span>
"""
        html += """                </div>
"""

        # Calculate max value for scaling
        max_slot_games = max(
            max(slots.values())
            for slots in report.time_slot_distribution.values()
        )

        for team_name in sorted(report.time_slot_distribution.keys()):
            slots = report.time_slot_distribution[team_name]

            html += f"""                <div class="bar-row">
                    <div class="bar-label">{team_name}</div>
                    <div class="bar-container">
"""

            for i, ts in enumerate(time_slots):
                count = slots.get(ts, 0)
                width = (count / max_slot_games) * 100 if max_slot_games > 0 else 0
                color = colors[i % len(colors)]
                html += f"""                        <div class="bar" style="width: {width}%; background: {color};" title="{ts}">{count}</div>
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

            html += f"""                <div class="bar-row">
                    <div class="bar-label">{team_name}</div>
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

            html += f"""                    <div class="bar-row">
                        <div class="bar-label">{team_name}</div>
                        <div class="bar-container">
                            <div class="bar bar-home" style="width: {first_width}%">{first_half}</div>
                            <div class="bar bar-away" style="width: {second_width}%">{second_half}</div>
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

    # Calculate expected matchups per pair (for 9 teams, 16 games each = 72 total, 36 pairs play 2x each)
    total_teams = len(teams)
    if total_teams > 1:
        expected_matchups = 2  # With 9 teams and 16 games each, each pair should play 2 times

    # Always show the matrix with color coding
    html += """                <div class="matchup-heatmap">
                    <table class="matchup-heatmap-table">
                        <thead>
                            <tr>
                                <th></th>
"""
    for team in teams:
        html += f"                                <th>{team}</th>\n"

    html += """                            </tr>
                        </thead>
                        <tbody>
"""

    for i, team1 in enumerate(teams):
        html += "                            <tr>\n"
        html += f"                                <td class='team-label'>{team1}</td>\n"

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
"""

    # Schedule rendering (toggle with SHOW_SCHEDULE flag)
    if SHOW_SCHEDULE:
        html += """
        <h2>Schedule</h2>
        <div class="schedule-grid">
"""

        # Build a lookup of games by (date, time, sheet)
        game_lookup: dict[tuple[date, time, str], ScheduledGame] = {}
        for game in games:
            key = (game.start_time.date(), game.start_time.time(), game.sheet_id)
            game_lookup[key] = game

        # Generate all slots and group by date
        all_slots = _generate_slots(config)
        slots_by_date: dict[date, list[GameSlot]] = {}
        for s in all_slots:
            if s.date not in slots_by_date:
                slots_by_date[s.date] = []
            slots_by_date[s.date].append(s)

        # Find the last date with any games scheduled
        last_game_date = max(game.start_time.date() for game in games) if games else None

        # Only show dates up to the last game
        if last_game_date:
            all_sorted_dates = [d for d in sorted(slots_by_date.keys()) if d <= last_game_date]
        else:
            all_sorted_dates = sorted(slots_by_date.keys())

        # Get all team names
        all_team_names = set(config.all_teams[0].name for _ in config.all_teams)
        all_team_names = sorted([team.name for team in config.all_teams])

        # Track teams with byes from previous week
        previous_bye_teams = set()

        for game_date in all_sorted_dates:
            date_slots = slots_by_date[game_date]
            # Sort slots by time, then sheet
            date_slots.sort(key=lambda s: (s.time, s.sheet_id))

            # Check if any games on this date
            games_on_date = [s for s in date_slots if (s.date, s.time, s.sheet_id) in game_lookup]

            if not games_on_date:
                # No games scheduled on this date - skip it entirely
                continue

            # Find which teams are playing on this date
            playing_teams = set()
            for slot in date_slots:
                key = (slot.date, slot.time, slot.sheet_id)
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
            <div class="date-header">{game_date.strftime('%A, %B %d, %Y')}</div>
            <div class="games-list">
"""

            for slot in date_slots:
                key = (slot.date, slot.time, slot.sheet_id)
                game = game_lookup.get(key)

                if game:
                    html += f"""                <div class="game-line">
                    <div class="game-time">{game.start_time.strftime('%H:%M')}</div>
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
