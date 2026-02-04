"""Tests for schedule generation library."""

from datetime import date, time
from pathlib import Path

import pytest

from score.scheduler import (
    ScheduleConfig,
    Team,
    Sheet,
    Division,
    load_config,
    generate_schedule,
    analyze_fairness,
    _generate_matchups,
    _generate_slots,
)


@pytest.fixture
def sample_config():
    """Create a sample schedule configuration for testing."""
    return ScheduleConfig(
        league_id="test-league",
        season_id="2025-2026",
        rink_id="test-rink",
        sheets=[
            Sheet(sheet_id="sheet-a", name="Sheet A"),
            Sheet(sheet_id="sheet-b", name="Sheet B"),
        ],
        divisions=[
            Division(
                division_id="div-a",
                games_per_team=12,
                teams=[
                    Team(registration_id="team-1", name="Team One", abbreviation="T1", division_id="div-a"),
                    Team(registration_id="team-2", name="Team Two", abbreviation="T2", division_id="div-a"),
                    Team(registration_id="team-3", name="Team Three", abbreviation="T3", division_id="div-a"),
                    Team(registration_id="team-4", name="Team Four", abbreviation="T4", division_id="div-a"),
                ],
            ),
        ],
        period_length_min=15,
        num_periods=3,
        game_type="regular",
        days_of_week=[6],  # Sunday
        start_date=date(2025, 10, 5),
        end_date=date(2026, 3, 29),
        blackout_dates=set(),
        time_slots=[time(18, 0), time(19, 30), time(21, 0)],
    )


@pytest.fixture
def small_config():
    """Create a smaller config for faster tests."""
    return ScheduleConfig(
        league_id="test-league",
        season_id="2025-2026",
        rink_id="test-rink",
        sheets=[
            Sheet(sheet_id="sheet-a", name="Sheet A"),
        ],
        divisions=[
            Division(
                division_id="div-a",
                games_per_team=4,
                teams=[
                    Team(registration_id="team-1", name="Team One", abbreviation="T1", division_id="div-a"),
                    Team(registration_id="team-2", name="Team Two", abbreviation="T2", division_id="div-a"),
                    Team(registration_id="team-3", name="Team Three", abbreviation="T3", division_id="div-a"),
                ],
            ),
        ],
        period_length_min=15,
        num_periods=3,
        game_type="regular",
        days_of_week=[6],  # Sunday
        start_date=date(2025, 10, 5),
        end_date=date(2025, 12, 28),
        blackout_dates=set(),
        time_slots=[time(18, 0), time(19, 30)],
    )


class TestLoadConfig:
    """Test config loading from YAML."""

    def test_load_valid_config(self, tmp_path):
        """Test loading a valid YAML config file."""
        config_content = """
league_id: "test-league"
season_id: "2025-2026"
rink_id: "test-rink"
sheets:
  - sheet_id: "sheet-a"
    name: "Sheet A"
divisions:
  - division_id: "div-a"
    games_per_team: 4
    teams:
      - registration_id: "team-1"
        name: "Team One"
        abbreviation: "T1"
      - registration_id: "team-2"
        name: "Team Two"
        abbreviation: "T2"
game_settings:
  period_length_min: 15
  num_periods: 3
  game_type: "regular"
schedule:
  days_of_week: ["sunday"]
  start_date: "2025-10-05"
  end_date: "2025-12-28"
  blackout_dates: []
  time_slots:
    - "18:00"
    - "19:30"
"""
        config_path = tmp_path / "test_config.yaml"
        config_path.write_text(config_content)

        config = load_config(config_path)

        assert config.league_id == "test-league"
        assert config.season_id == "2025-2026"
        assert len(config.all_teams) == 2
        assert len(config.sheets) == 1
        assert config.divisions[0].games_per_team == 4
        assert len(config.time_slots) == 2
        assert 6 in config.days_of_week  # Sunday = 6

    def test_load_config_with_blackout_dates(self, tmp_path):
        """Test loading config with blackout dates."""
        config_content = """
league_id: "test"
season_id: "2025"
rink_id: "rink"
sheets:
  - sheet_id: "a"
    name: "A"
divisions:
  - division_id: "div"
    games_per_team: 2
    teams:
      - registration_id: "t1"
        name: "T1"
        abbreviation: "T1"
      - registration_id: "t2"
        name: "T2"
        abbreviation: "T2"
game_settings:
  period_length_min: 15
  num_periods: 3
  game_type: "regular"
schedule:
  days_of_week: ["sunday"]
  start_date: "2025-10-05"
  end_date: "2025-12-28"
  blackout_dates:
    - "2025-11-30"
    - "2025-12-25"
  time_slots:
    - "18:00"
"""
        config_path = tmp_path / "test_config.yaml"
        config_path.write_text(config_content)

        config = load_config(config_path)

        assert len(config.blackout_dates) == 2
        assert date(2025, 11, 30) in config.blackout_dates
        assert date(2025, 12, 25) in config.blackout_dates


class TestSlotGeneration:
    """Test game slot generation."""

    def test_generates_correct_number_of_slots(self, small_config):
        """Test that slots are generated for all valid dates/times/sheets."""
        slots = _generate_slots(small_config)

        # Count Sundays between Oct 5, 2025 and Dec 28, 2025
        # Oct: 5, 12, 19, 26 = 4
        # Nov: 2, 9, 16, 23, 30 = 5
        # Dec: 7, 14, 21, 28 = 4
        # Total: 13 Sundays
        # With 2 time slots and 1 sheet = 13 * 2 * 1 = 26 slots
        expected_sundays = 13
        expected_slots = expected_sundays * len(small_config.time_slots) * len(small_config.sheets)
        assert len(slots) == expected_slots

    def test_slots_respect_blackout_dates(self, small_config):
        """Test that blackout dates are excluded from slots."""
        small_config.blackout_dates = {date(2025, 10, 12)}  # Second Sunday
        slots = _generate_slots(small_config)

        # Should have one fewer Sunday worth of slots
        for s in slots:
            assert s.date != date(2025, 10, 12)


class TestMatchupGeneration:
    """Test matchup generation."""

    def test_generates_all_matchups(self, small_config):
        """Test that all team pairings are generated."""
        matchups = _generate_matchups(small_config)

        # With 3 teams and games_per_team=4:
        # Each team plays each of 2 opponents multiple times
        # Matchups: 3 teams * 2 opponents * games_per_team copies = 3 * 2 * 4 = 24
        div = small_config.divisions[0]
        expected = len(div.teams) * (len(div.teams) - 1) * div.games_per_team
        assert len(matchups) == expected

    def test_no_self_matchups(self, small_config):
        """Test that no team plays itself."""
        matchups = _generate_matchups(small_config)

        for m in matchups:
            assert m.home_team.registration_id != m.away_team.registration_id


class TestScheduleGeneration:
    """Test full schedule generation."""

    def test_generates_correct_number_of_games(self, small_config):
        """Test that correct number of games are generated."""
        games = generate_schedule(small_config)

        # Total games = teams * games_per_team / 2 (since each game involves 2 teams)
        div = small_config.divisions[0]
        expected_games = len(div.teams) * div.games_per_team // 2
        assert len(games) == expected_games

    def test_each_team_plays_correct_number(self, small_config):
        """Test that each team plays exactly games_per_team games."""
        games = generate_schedule(small_config)

        div = small_config.divisions[0]
        team_game_counts = {t.registration_id: 0 for t in div.teams}
        for game in games:
            team_game_counts[game.home_registration_id] += 1
            team_game_counts[game.away_registration_id] += 1

        for team_id, count in team_game_counts.items():
            assert count == div.games_per_team, f"Team {team_id} played {count} games"

    def test_no_double_booking(self, small_config):
        """Test that no slot has multiple games."""
        games = generate_schedule(small_config)

        slot_usage = {}
        for game in games:
            key = (game.start_time, game.sheet_id)
            assert key not in slot_usage, f"Double booking at {key}"
            slot_usage[key] = game

    def test_one_game_per_team_per_day(self, small_config):
        """Test that no team plays multiple games on the same day."""
        games = generate_schedule(small_config)

        div = small_config.divisions[0]
        team_dates = {t.registration_id: set() for t in div.teams}
        for game in games:
            game_date = game.start_time.date()

            assert game_date not in team_dates[game.home_registration_id], \
                f"{game.home_team} plays multiple games on {game_date}"
            team_dates[game.home_registration_id].add(game_date)

            assert game_date not in team_dates[game.away_registration_id], \
                f"{game.away_team} plays multiple games on {game_date}"
            team_dates[game.away_registration_id].add(game_date)


class TestFairnessAnalysis:
    """Test fairness analysis."""

    def test_time_slot_balance(self, small_config):
        """Test that time slots are balanced within ±1."""
        games = generate_schedule(small_config)
        report = analyze_fairness(games, small_config)

        div = small_config.divisions[0]
        expected_per_slot = div.games_per_team // len(small_config.time_slots)

        for team, slots in report.time_slot_distribution.items():
            for slot_time, count in slots.items():
                assert abs(count - expected_per_slot) <= 1, \
                    f"{team} has {count} games at {slot_time}, expected ~{expected_per_slot}"

    def test_home_away_balance(self, small_config):
        """Test that home/away games are balanced within ±1."""
        games = generate_schedule(small_config)
        report = analyze_fairness(games, small_config)

        div = small_config.divisions[0]
        for team, (home, away) in report.home_away_balance.items():
            assert abs(home - away) <= 1, \
                f"{team} has {home} home and {away} away games"
            assert home + away == div.games_per_team, \
                f"{team} total games ({home} + {away}) != {div.games_per_team}"

    def test_report_summary_format(self, small_config):
        """Test that report summary is formatted correctly."""
        games = generate_schedule(small_config)
        report = analyze_fairness(games, small_config)
        summary = report.summary()

        assert "Fairness Report" in summary
        assert "Time Slot Distribution" in summary
        assert "Sheet Distribution" in summary
        assert "Home/Away Balance" in summary
        assert "Opponent Distribution" in summary


class TestIntegration:
    """Integration tests with example config file."""

    def test_example_config_generates_valid_schedule(self):
        """Test that the example config file generates a valid schedule."""
        example_path = Path(__file__).parent.parent / "examples" / "schedule.yaml"
        if not example_path.exists():
            pytest.skip("Example config not found")

        config = load_config(example_path)
        games = generate_schedule(config)

        # Basic validation - sum games across all divisions
        expected_games = sum(len(d.teams) * d.games_per_team // 2 for d in config.divisions)
        assert len(games) == expected_games

        # Check fairness
        report = analyze_fairness(games, config)
        for team, (home, away) in report.home_away_balance.items():
            assert abs(home - away) <= 1, f"{team} home/away imbalance"
