#!/usr/bin/env python3
"""
Test script for Cloud API Simulator

This script demonstrates all the API endpoints with example requests.
"""

import requests
from datetime import datetime, timezone

BASE_URL = "http://localhost:8001"


def print_section(title):
    """Print a section header."""
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60 + "\n")


def test_get_schedule():
    """Test GET schedule endpoint."""
    print_section("1. Testing GET Schedule")

    url = f"{BASE_URL}/v1/rinks/rink-alpha/schedule"
    print(f"GET {url}")

    response = requests.get(url)
    print(f"Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        print(f"\nSchedule Version: {data['schedule_version']}")
        print(f"Number of Games: {len(data['games'])}")
        print("\nGames:")
        for game in data['games']:
            print(f"  - {game['game_id']}: {game['home_team']} vs {game['away_team']}")
            print(f"    Start: {game['start_time']}, Period: {game['period_length_min']} min")
    else:
        print(f"Error: {response.text}")


def test_post_events():
    """Test POST events endpoint."""
    print_section("2. Testing POST Events")

    game_id = "game-001"
    url = f"{BASE_URL}/v1/games/{game_id}/events"
    print(f"POST {url}")

    payload = {
        "device_id": "test-device-001",
        "session_id": "test-session-123",
        "events": [
            {
                "event_id": "evt-test-001",
                "seq": 1,
                "type": "CLOCK_SET",
                "ts_local": datetime.now(timezone.utc).isoformat(),
                "payload": {"seconds": 1200}
            },
            {
                "event_id": "evt-test-002",
                "seq": 2,
                "type": "GAME_STARTED",
                "ts_local": datetime.now(timezone.utc).isoformat(),
                "payload": {}
            },
            {
                "event_id": "evt-test-003",
                "seq": 3,
                "type": "GOAL",
                "ts_local": datetime.now(timezone.utc).isoformat(),
                "payload": {"team": "home", "player": 7}
            }
        ]
    }

    print(f"\nPosting {len(payload['events'])} events...")
    response = requests.post(url, json=payload)
    print(f"Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        print(f"\nAcked through: {data['acked_through']}")
        print(f"Server time: {data['server_time']}")

        # Test idempotency - post same events again
        print("\n--- Testing Idempotency ---")
        print("Posting same events again...")
        response2 = requests.post(url, json=payload)
        data2 = response2.json()
        print(f"Status: {response2.status_code}")
        print(f"Acked through: {data2['acked_through']}")
        print("✓ Idempotency works - no duplicates created")
    else:
        print(f"Error: {response.text}")


def test_post_heartbeat():
    """Test POST heartbeat endpoint."""
    print_section("3. Testing POST Heartbeat")

    url = f"{BASE_URL}/v1/heartbeat"
    print(f"POST {url}")

    payload = {
        "device_id": "test-device-001",
        "current_game_id": "game-001",
        "game_state": "RUNNING",
        "clock_running": True,
        "clock_value_ms": 352000,
        "last_event_seq": 3,
        "app_version": "1.0.0-test",
        "ts_local": datetime.now(timezone.utc).isoformat()
    }

    print(f"\nSending heartbeat for device: {payload['device_id']}")
    response = requests.post(url, json=payload)
    print(f"Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        print(f"\nResponse: {data['status']}")
        print(f"Server time: {data['server_time']}")
    else:
        print(f"Error: {response.text}")


def test_admin_endpoints():
    """Test admin/debug endpoints."""
    print_section("4. Testing Admin Endpoints")

    # Get latest heartbeats
    print("GET /admin/heartbeats/latest")
    response = requests.get(f"{BASE_URL}/admin/heartbeats/latest")
    print(f"Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        print(f"\nLatest Heartbeats ({len(data['heartbeats'])}):")
        for hb in data['heartbeats']:
            print(f"  - Device: {hb['device_id']}")
            print(f"    Game: {hb['current_game_id']}, State: {hb['game_state']}")
            print(f"    Version: {hb['app_version']}")

    # Get game events
    print("\n" + "-" * 60)
    game_id = "game-001"
    print(f"\nGET /admin/events/{game_id}")
    response = requests.get(f"{BASE_URL}/admin/events/{game_id}")
    print(f"Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        print(f"\nGame: {data['game_id']}")
        print(f"Event Count: {data['event_count']}")
        if data['event_count'] > 0:
            print("\nEvents:")
            for evt in data['events'][:5]:  # Show first 5
                print(f"  - Seq {evt['seq']}: {evt['type']} ({evt['device_id']})")
            if data['event_count'] > 5:
                print(f"  ... and {data['event_count'] - 5} more")


def main():
    """Run all tests."""
    print("\n" + "🌩️  Cloud API Simulator - Test Script  🌩️".center(60))
    print("=" * 60)
    print(f"Base URL: {BASE_URL}")

    try:
        # Check if server is running
        response = requests.get(f"{BASE_URL}/docs", timeout=2)
        if response.status_code != 200:
            print("\n❌ Cloud API server is not responding")
            print("   Start it with: make run-cloud")
            return
    except requests.exceptions.ConnectionError:
        print("\n❌ Cannot connect to Cloud API server")
        print("   Start it with: make run-cloud")
        return

    print("✓ Server is running\n")

    # Run tests
    test_get_schedule()
    test_post_events()
    test_post_heartbeat()
    test_admin_endpoints()

    print_section("✅ All Tests Complete")
    print("Check interactive docs at: http://localhost:8001/docs")
    print()


if __name__ == "__main__":
    main()
