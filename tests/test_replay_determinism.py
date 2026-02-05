"""
Test that event replay is deterministic.

This addresses critical issue #2: Non-deterministic event replay.
Replaying the same events must always produce the same state,
regardless of when the replay happens.
"""
import time
from score.state import replay_events


def test_replay_is_deterministic_without_current_time():
    """Test that replay produces identical results when called at different times."""
    base_time = int(time.time()) - 1000

    events = [
        {
            "type": "CLOCK_SET",
            "payload": '{"seconds": 900}',
            "created_at": base_time,
        },
        {
            "type": "GAME_STARTED",
            "payload": "{}",
            "created_at": base_time + 10,
        },
    ]

    # Replay at time T
    result1 = replay_events(events)

    # Wait a bit
    time.sleep(0.1)

    # Replay at time T+0.1
    result2 = replay_events(events)

    # Results should be IDENTICAL
    assert result1["seconds"] == result2["seconds"], \
        f"Non-deterministic: {result1['seconds']} != {result2['seconds']}"
    assert result1["running"] == result2["running"]
    assert result1["last_update"] == result2["last_update"]
    assert result1["home_score"] == result2["home_score"]
    assert result1["away_score"] == result2["away_score"]


def test_replay_with_paused_game_is_deterministic():
    """Test deterministic replay when game is paused."""
    base_time = int(time.time()) - 1000

    events = [
        {
            "type": "CLOCK_SET",
            "payload": '{"seconds": 900}',
            "created_at": base_time,
        },
        {
            "type": "GAME_STARTED",
            "payload": "{}",
            "created_at": base_time + 10,
        },
        {
            "type": "GAME_PAUSED",
            "payload": "{}",
            "created_at": base_time + 70,  # 60 seconds elapsed
        },
    ]

    result1 = replay_events(events)
    time.sleep(0.1)
    result2 = replay_events(events)

    # Should both show 840 seconds (900 - 60)
    assert result1["seconds"] == 840
    assert result2["seconds"] == 840
    assert result1["seconds"] == result2["seconds"]
    assert result1["running"] is False
    assert result2["running"] is False


def test_replay_with_running_game_is_deterministic_without_current_time():
    """Test that a running game WITHOUT current_time is deterministic."""
    base_time = int(time.time()) - 1000

    events = [
        {
            "type": "CLOCK_SET",
            "payload": '{"seconds": 900}',
            "created_at": base_time,
        },
        {
            "type": "GAME_STARTED",
            "payload": "{}",
            "created_at": base_time + 10,
        },
        # Game is still running (no pause event)
    ]

    result1 = replay_events(events)
    time.sleep(0.1)
    result2 = replay_events(events)

    # Without current_time, both should show 900 seconds (no elapsed time calculated)
    assert result1["seconds"] == 900
    assert result2["seconds"] == 900
    assert result1["running"] is True
    assert result2["running"] is True
    assert result1["last_update"] == base_time + 10
    assert result2["last_update"] == base_time + 10


def test_replay_with_current_time_calculates_elapsed():
    """Test that providing current_time calculates elapsed time for display."""
    base_time = int(time.time()) - 1000

    events = [
        {
            "type": "CLOCK_SET",
            "payload": '{"seconds": 900}',
            "created_at": base_time,
        },
        {
            "type": "GAME_STARTED",
            "payload": "{}",
            "created_at": base_time + 10,
        },
    ]

    # Explicitly provide current_time
    current_time = base_time + 110  # 100 seconds after start
    result = replay_events(events, current_time=current_time)

    # Should calculate elapsed time: 900 - 100 = 800
    assert 790 <= result["seconds"] <= 810  # Allow small tolerance
    assert result["running"] is True


def test_replay_matches_regardless_of_observation_time():
    """Test that deterministic replay works across multiple observation times."""
    base_time = int(time.time()) - 2000

    events = [
        {"type": "CLOCK_SET", "payload": '{"seconds": 1200}', "created_at": base_time},
        {"type": "GAME_STARTED", "payload": "{}", "created_at": base_time + 5},
        {"type": "GAME_PAUSED", "payload": "{}", "created_at": base_time + 35},  # 30s elapsed
        {"type": "GAME_STARTED", "payload": "{}", "created_at": base_time + 100},
        {"type": "GAME_PAUSED", "payload": "{}", "created_at": base_time + 150},  # 50s elapsed
    ]

    # Replay multiple times at different moments
    results = []
    for _ in range(3):
        result = replay_events(events)
        results.append(result)
        time.sleep(0.05)

    # All results should be identical
    for i in range(1, len(results)):
        assert results[i]["seconds"] == results[0]["seconds"], \
            f"Replay {i} differs from replay 0"
        assert results[i]["running"] == results[0]["running"]
        assert results[i]["last_update"] == results[0]["last_update"]

    # Should show 1120 seconds (1200 - 30 - 50)
    assert results[0]["seconds"] == 1120
    assert results[0]["running"] is False


def test_cloud_and_app_get_same_state():
    """Test that cloud and app replaying same events get identical state."""
    base_time = int(time.time()) - 500

    # App uses created_at field
    app_events = [
        {"type": "CLOCK_SET", "payload": '{"seconds": 600}', "created_at": base_time},
        {"type": "GAME_STARTED", "payload": "{}", "created_at": base_time + 5},
        {"type": "GAME_PAUSED", "payload": "{}", "created_at": base_time + 125},  # 120s elapsed
    ]

    # Cloud uses received_at field (state.py handles both)
    cloud_events = [
        {"type": "CLOCK_SET", "payload": '{"seconds": 600}', "received_at": base_time},
        {"type": "GAME_STARTED", "payload": "{}", "received_at": base_time + 5},
        {"type": "GAME_PAUSED", "payload": "{}", "received_at": base_time + 125},
    ]

    app_result = replay_events(app_events)
    cloud_result = replay_events(cloud_events)

    # Both should show 480 seconds (600 - 120)
    assert app_result["seconds"] == cloud_result["seconds"] == 480
    assert app_result["running"] == cloud_result["running"] is False
    assert app_result["home_score"] == cloud_result["home_score"] == 0
