"""
Fast schedule generation using greedy construction + local search.

Much faster than CP-SAT solver, produces good (not provably optimal) schedules.
"""

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from score.scheduler import (
    ScheduleConfig,
    ScheduledGame,
    GameSlot,
    Matchup,
    FairnessReport,
    load_config,
    analyze_fairness,
    _generate_slots,
    _generate_matchups,
    _write_html_schedule,
    _print_full_schedule,
)


@dataclass
class ScheduleState:
    """Mutable state for the schedule being built."""
    # slot_id -> assigned matchup (or None if unused)
    assignments: dict[int, Matchup | None] = field(default_factory=dict)
    # matchup_id -> slot_id (or None if unscheduled)
    matchup_slots: dict[int, int | None] = field(default_factory=dict)
    # team_id -> set of dates where team has a game
    team_dates: dict[str, set] = field(default_factory=dict)
    # team_id -> number of games scheduled
    team_game_count: dict[str, int] = field(default_factory=dict)
    # team_id -> number of home games
    team_home_count: dict[str, int] = field(default_factory=dict)
    # (team1_id, team2_id) -> number of games between pair (ordered pair)
    pair_game_count: dict[tuple[str, str], int] = field(default_factory=dict)
    # team_id -> {time_slot_str -> count}
    team_time_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    # team_id -> {sheet_id -> count}
    team_sheet_counts: dict[str, dict[str, int]] = field(default_factory=dict)


def _init_state(config: ScheduleConfig, slots: list[GameSlot]) -> ScheduleState:
    """Initialize empty schedule state."""
    state = ScheduleState()

    # Initialize slot assignments
    for s in slots:
        state.assignments[s.slot_id] = None

    # Initialize team tracking
    for team in config.all_teams:
        tid = team.registration_id
        state.team_dates[tid] = set()
        state.team_game_count[tid] = 0
        state.team_home_count[tid] = 0
        state.team_time_counts[tid] = {}
        state.team_sheet_counts[tid] = {}

    return state


def _get_expected_games_per_pair(config: ScheduleConfig, division_id: str) -> int:
    """Calculate expected games per pair for a division."""
    div = next(d for d in config.divisions if d.division_id == division_id)
    num_teams = len(div.teams)
    if num_teams <= 1:
        return 0
    # Total games = teams * games_per_team / 2
    # Number of pairs = teams * (teams-1) / 2
    # Expected per pair = total_games / num_pairs = games_per_team / (teams - 1)
    return div.games_per_team // (num_teams - 1)


def _can_assign(state: ScheduleState, matchup: Matchup, slot: GameSlot, config: ScheduleConfig) -> bool:
    """Check if matchup can be assigned to slot (hard constraints)."""
    home_id = matchup.home_team.registration_id
    away_id = matchup.away_team.registration_id

    # Slot must be free
    if state.assignments.get(slot.slot_id) is not None:
        return False

    # Teams can't play twice on same date
    if slot.date in state.team_dates[home_id]:
        return False
    if slot.date in state.team_dates[away_id]:
        return False

    # Find division for this matchup to get games_per_team
    div = next(d for d in config.divisions if d.division_id == matchup.division_id)

    # Teams can't exceed games_per_team
    if state.team_game_count[home_id] >= div.games_per_team:
        return False
    if state.team_game_count[away_id] >= div.games_per_team:
        return False

    # Pair can't exceed expected games per pair
    expected_per_pair = _get_expected_games_per_pair(config, matchup.division_id)
    pair_key = (min(home_id, away_id), max(home_id, away_id))
    if state.pair_game_count.get(pair_key, 0) >= expected_per_pair:
        return False

    return True


def _assign(state: ScheduleState, matchup: Matchup, slot: GameSlot):
    """Assign matchup to slot, updating all tracking."""
    home_id = matchup.home_team.registration_id
    away_id = matchup.away_team.registration_id
    time_str = slot.time.strftime("%H:%M")

    state.assignments[slot.slot_id] = matchup
    state.matchup_slots[matchup.matchup_id] = slot.slot_id

    state.team_dates[home_id].add(slot.date)
    state.team_dates[away_id].add(slot.date)

    state.team_game_count[home_id] += 1
    state.team_game_count[away_id] += 1

    state.team_home_count[home_id] += 1

    # Track pair games
    pair_key = (min(home_id, away_id), max(home_id, away_id))
    state.pair_game_count[pair_key] = state.pair_game_count.get(pair_key, 0) + 1

    # Track time slot distribution
    state.team_time_counts[home_id][time_str] = state.team_time_counts[home_id].get(time_str, 0) + 1
    state.team_time_counts[away_id][time_str] = state.team_time_counts[away_id].get(time_str, 0) + 1

    # Track sheet distribution
    state.team_sheet_counts[home_id][slot.sheet_id] = state.team_sheet_counts[home_id].get(slot.sheet_id, 0) + 1
    state.team_sheet_counts[away_id][slot.sheet_id] = state.team_sheet_counts[away_id].get(slot.sheet_id, 0) + 1


def _unassign(state: ScheduleState, slot: GameSlot):
    """Remove assignment from slot, updating all tracking."""
    matchup = state.assignments.get(slot.slot_id)
    if matchup is None:
        return

    home_id = matchup.home_team.registration_id
    away_id = matchup.away_team.registration_id
    time_str = slot.time.strftime("%H:%M")

    state.assignments[slot.slot_id] = None
    state.matchup_slots[matchup.matchup_id] = None

    state.team_dates[home_id].discard(slot.date)
    state.team_dates[away_id].discard(slot.date)

    state.team_game_count[home_id] -= 1
    state.team_game_count[away_id] -= 1

    state.team_home_count[home_id] -= 1

    # Track pair games
    pair_key = (min(home_id, away_id), max(home_id, away_id))
    state.pair_game_count[pair_key] = state.pair_game_count.get(pair_key, 1) - 1

    # Track time slot distribution
    state.team_time_counts[home_id][time_str] -= 1
    state.team_time_counts[away_id][time_str] -= 1

    # Track sheet distribution
    state.team_sheet_counts[home_id][slot.sheet_id] -= 1
    state.team_sheet_counts[away_id][slot.sheet_id] -= 1


def _score_assignment(state: ScheduleState, matchup: Matchup, slot: GameSlot, config: ScheduleConfig) -> float:
    """Score how good this assignment is (higher = better)."""
    home_id = matchup.home_team.registration_id
    away_id = matchup.away_team.registration_id
    time_str = slot.time.strftime("%H:%M")

    score = 0.0

    # Prefer teams with fewer games (balance game count)
    score -= (state.team_game_count[home_id] + state.team_game_count[away_id]) * 10

    # Strongly prefer home/away balance
    # Home team should have fewer home games than away games currently
    home_home = state.team_home_count[home_id]
    home_away = state.team_game_count[home_id] - home_home
    away_home = state.team_home_count[away_id]
    away_away = state.team_game_count[away_id] - away_home

    # Reward if home team needs home games and away team needs away games
    home_needs_home = home_away > home_home
    away_needs_away = away_home > away_away

    if home_needs_home:
        score += 50 * config.solver.weight_home_away
    if away_needs_away:
        score += 50 * config.solver.weight_home_away

    # Penalize if home team already has too many home games
    if home_home > home_away + 1:
        score -= 100 * config.solver.weight_home_away
    # Penalize if away team already has too many away games
    if away_away > away_home + 1:
        score -= 100 * config.solver.weight_home_away

    # Balance time slots
    home_time_count = state.team_time_counts[home_id].get(time_str, 0)
    away_time_count = state.team_time_counts[away_id].get(time_str, 0)
    score -= (home_time_count + away_time_count) * config.solver.weight_time_slot

    # Balance sheets
    home_sheet_count = state.team_sheet_counts[home_id].get(slot.sheet_id, 0)
    away_sheet_count = state.team_sheet_counts[away_id].get(slot.sheet_id, 0)
    score -= (home_sheet_count + away_sheet_count) * config.solver.weight_sheet

    # Strongly prefer pairs that still need games
    pair_key = (min(home_id, away_id), max(home_id, away_id))
    pair_count = state.pair_game_count.get(pair_key, 0)
    expected_per_pair = _get_expected_games_per_pair(config, matchup.division_id)

    # Big bonus for pairs that haven't played yet
    if pair_count == 0:
        score += 500
    # Smaller bonus for pairs that need one more game
    elif pair_count < expected_per_pair:
        score += 200

    return score


def _greedy_construct(
    config: ScheduleConfig,
    slots: list[GameSlot],
    matchups: list[Matchup],
) -> ScheduleState:
    """
    Build initial schedule using round-robin pairing.

    Strategy: Generate a round-robin tournament structure that guarantees
    all pairs play exactly the required number of times.
    """
    state = _init_state(config, slots)

    # Sort slots by date (pack games early)
    sorted_slots = sorted(slots, key=lambda s: (s.date, s.time, s.slot_id))

    # Group matchups by pair (undirected) and direction
    matchups_by_pair: dict[frozenset, list[Matchup]] = {}
    for m in matchups:
        pair = frozenset([m.home_team.registration_id, m.away_team.registration_id])
        if pair not in matchups_by_pair:
            matchups_by_pair[pair] = []
        matchups_by_pair[pair].append(m)

    # Track used matchup IDs
    used_matchup_ids: set[int] = set()

    # For each division, create a round-robin schedule
    for division in config.divisions:
        expected_per_pair = _get_expected_games_per_pair(config, division.division_id)
        teams = list(division.teams)
        n = len(teams)

        # Generate all required pair games
        required_games: list[tuple[str, str]] = []  # (team1_id, team2_id)
        for i, t1 in enumerate(teams):
            for t2 in teams[i+1:]:
                for _ in range(expected_per_pair):
                    required_games.append((t1.registration_id, t2.registration_id))

        # Shuffle to randomize which games go where
        import random
        random.shuffle(required_games)

        # Assign games to slots
        for t1_id, t2_id in required_games:
            pair = frozenset([t1_id, t2_id])
            pair_matchups = matchups_by_pair.get(pair, [])

            # Find best available slot for this game
            best_slot = None
            best_matchup = None
            best_score = float('-inf')

            for slot in sorted_slots:
                if state.assignments.get(slot.slot_id) is not None:
                    continue

                for m in pair_matchups:
                    if m.matchup_id in used_matchup_ids:
                        continue

                    home_id = m.home_team.registration_id
                    away_id = m.away_team.registration_id

                    # Check constraints (except pair count since we're forcing this)
                    if slot.date in state.team_dates[home_id]:
                        continue
                    if slot.date in state.team_dates[away_id]:
                        continue

                    div = next(d for d in config.divisions if d.division_id == m.division_id)
                    if state.team_game_count[home_id] >= div.games_per_team:
                        continue
                    if state.team_game_count[away_id] >= div.games_per_team:
                        continue

                    score = _score_assignment(state, m, slot, config)
                    if score > best_score:
                        best_score = score
                        best_slot = slot
                        best_matchup = m

            if best_matchup and best_slot:
                _assign(state, best_matchup, best_slot)
                used_matchup_ids.add(best_matchup.matchup_id)

    scheduled = sum(1 for m in state.assignments.values() if m is not None)
    print(f"    Round-robin scheduled: {scheduled} games")

    return state


def _calculate_fairness_score(state: ScheduleState, config: ScheduleConfig) -> float:
    """Calculate overall fairness score (lower = better)."""
    score = 0.0

    for team in config.all_teams:
        tid = team.registration_id
        games = state.team_game_count[tid]
        if games == 0:
            continue

        # Home/away imbalance
        home = state.team_home_count[tid]
        away = games - home
        imbalance = abs(home - away)
        score += imbalance * config.solver.weight_home_away

        # Time slot imbalance
        time_counts = list(state.team_time_counts[tid].values())
        if time_counts:
            avg_time = sum(time_counts) / len(time_counts)
            time_var = sum((c - avg_time) ** 2 for c in time_counts)
            score += time_var * config.solver.weight_time_slot

        # Sheet imbalance
        sheet_counts = list(state.team_sheet_counts[tid].values())
        if sheet_counts:
            avg_sheet = sum(sheet_counts) / len(sheet_counts)
            sheet_var = sum((c - avg_sheet) ** 2 for c in sheet_counts)
            score += sheet_var * config.solver.weight_sheet

    # Opponent variety
    for pair_key, count in state.pair_game_count.items():
        # Penalize deviation from expected
        expected = 2  # Rough estimate
        deviation = abs(count - expected)
        score += deviation * config.solver.weight_opponent

    return score


def _local_search(
    state: ScheduleState,
    config: ScheduleConfig,
    slots: list[GameSlot],
    matchups: list[Matchup],
    max_iterations: int = 10000,
    max_no_improve: int = 1000,
) -> ScheduleState:
    """Improve schedule using local search (swap moves)."""

    # Build slot lookup
    slot_by_id = {s.slot_id: s for s in slots}

    # Get assigned slots
    assigned_slot_ids = [sid for sid, m in state.assignments.items() if m is not None]

    best_score = _calculate_fairness_score(state, config)
    no_improve_count = 0

    print(f"  Starting local search (score={best_score:.1f})...")

    for iteration in range(max_iterations):
        if no_improve_count >= max_no_improve:
            print(f"  Stopping after {iteration} iterations (no improvement for {max_no_improve})")
            break

        # Pick two random assigned slots and try swapping
        if len(assigned_slot_ids) < 2:
            break

        idx1, idx2 = random.sample(range(len(assigned_slot_ids)), 2)
        slot1_id = assigned_slot_ids[idx1]
        slot2_id = assigned_slot_ids[idx2]

        slot1 = slot_by_id[slot1_id]
        slot2 = slot_by_id[slot2_id]

        matchup1 = state.assignments[slot1_id]
        matchup2 = state.assignments[slot2_id]

        if matchup1 is None or matchup2 is None:
            continue

        # Try swap
        _unassign(state, slot1)
        _unassign(state, slot2)

        # Check if swap is valid
        can_swap = (
            _can_assign(state, matchup1, slot2, config) and
            _can_assign(state, matchup2, slot1, config)
        )

        if can_swap:
            _assign(state, matchup1, slot2)
            _assign(state, matchup2, slot1)

            new_score = _calculate_fairness_score(state, config)

            if new_score < best_score:
                # Accept improvement
                best_score = new_score
                no_improve_count = 0
                # Update assigned_slot_ids (slots haven't changed, just matchups)
                if iteration % 500 == 0:
                    print(f"    [{iteration}] Improved: score={best_score:.1f}")
            else:
                # Reject, undo swap
                _unassign(state, slot1)
                _unassign(state, slot2)
                _assign(state, matchup1, slot1)
                _assign(state, matchup2, slot2)
                no_improve_count += 1
        else:
            # Can't swap, restore original
            _assign(state, matchup1, slot1)
            _assign(state, matchup2, slot2)
            no_improve_count += 1

    print(f"  Final score: {best_score:.1f}")
    return state


def _state_to_games(
    state: ScheduleState,
    slots: list[GameSlot],
    config: ScheduleConfig,
) -> list[ScheduledGame]:
    """Convert schedule state to list of ScheduledGame."""
    slot_by_id = {s.slot_id: s for s in slots}
    games = []

    for slot_id, matchup in state.assignments.items():
        if matchup is None:
            continue

        slot = slot_by_id[slot_id]
        start_time = datetime.combine(slot.date, slot.time)
        game_id = str(uuid.uuid4())[:8]

        games.append(ScheduledGame(
            game_id=game_id,
            division_id=matchup.division_id,
            home_registration_id=matchup.home_team.registration_id,
            away_registration_id=matchup.away_team.registration_id,
            home_team=matchup.home_team.name,
            away_team=matchup.away_team.name,
            home_abbrev=matchup.home_team.abbreviation,
            away_abbrev=matchup.away_team.abbreviation,
            sheet_id=slot.sheet_id,
            rink_id=config.rink_id,
            start_time=start_time,
            period_length_min=config.period_length_min,
            num_periods=config.num_periods,
            game_type=config.game_type,
        ))

    games.sort(key=lambda g: g.start_time)
    return games


def generate_schedule_fast(
    config: ScheduleConfig,
    max_iterations: int = 10000,
    max_no_improve: int = 1000,
) -> list[ScheduledGame]:
    """
    Generate schedule using greedy construction + local search.

    Much faster than CP-SAT, produces good (not optimal) schedules.
    """
    print("Fast scheduler: generating slots and matchups...")
    slots = _generate_slots(config)
    matchups = _generate_matchups(config)

    total_games = sum(len(d.teams) * d.games_per_team // 2 for d in config.divisions)
    print(f"  Slots: {len(slots)}, Matchups: {len(matchups)}, Target games: {total_games}")

    print("Fast scheduler: greedy construction...")
    state = _greedy_construct(config, slots, matchups)

    scheduled = sum(1 for m in state.assignments.values() if m is not None)
    print(f"  Greedy scheduled {scheduled} games")

    print("Fast scheduler: local search optimization...")
    state = _local_search(state, config, slots, matchups, max_iterations, max_no_improve)

    return _state_to_games(state, slots, config)


def main():
    """Command-line interface for fast schedule generation."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: score-schedule-fast <config.yaml> [--html output.html]")
        sys.exit(1)

    config_path = Path(sys.argv[1])
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)

    # Check for HTML output flag, default to config name + .html
    if "--html" in sys.argv:
        html_idx = sys.argv.index("--html")
        if html_idx + 1 < len(sys.argv):
            html_output = Path(sys.argv[html_idx + 1])
        else:
            print("Error: --html flag requires output filename")
            sys.exit(1)
    else:
        # Default: same name as config but .html extension
        html_output = config_path.with_suffix('.html')

    print(f"Loading config from: {config_path}")
    config = load_config(config_path)

    div_names = ", ".join(d.division_id for d in config.divisions)
    print(f"\nGenerating schedule for: {config.league_id} - {config.season_id}")
    print(f"Divisions: {div_names}")

    import time
    start = time.time()
    games = generate_schedule_fast(config)
    elapsed = time.time() - start

    print(f"\nGenerated {len(games)} games in {elapsed:.2f}s")

    report = analyze_fairness(games, config)
    print(f"\n{report.summary()}")

    # Print full schedule
    _print_full_schedule(games, config)

    # Always generate HTML output
    _write_html_schedule(games, config, report, html_output)
    print(f"\nHTML schedule written to: {html_output}")
    print(f"Open in browser: file://{html_output.absolute()}")


if __name__ == "__main__":
    main()
