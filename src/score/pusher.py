import json
import logging
import signal
import sqlite3
import time
from abc import ABC, abstractmethod

# Set up logger for this module
logger = logging.getLogger("score.pusher")


class BaseEventPusher(ABC):
    """
    Base class for event pushers.

    Handles common functionality:
    - Database operations (querying events, tracking deliveries)
    - Main run loop with graceful shutdown
    - Retry logic

    Subclasses implement deliver() for specific destination types.
    """

    def __init__(self, db_path, destination):
        """
        Initialize the event pusher.

        Args:
            db_path: Path to SQLite database
            destination: Destination name for tracking (e.g., "events.log", "webhook:prod")
        """
        self.db_path = db_path
        self.destination = destination
        self.shutdown_requested = False

        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, _signum, _frame):
        """Handle shutdown signals gracefully."""
        logger.info("Shutdown signal received. Finishing current batch...")
        self.shutdown_requested = True

    def _get_db(self):
        """Get database connection with timeout for lock handling."""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def get_unprocessed_events(self):
        """
        Get events that haven't been delivered or failed delivery.

        Returns:
            List of event rows (pending or failed)
        """
        db = self._get_db()
        rows = db.execute("""
            SELECT e.* FROM events e
            LEFT JOIN deliveries d ON e.id = d.event_id AND d.destination = ?
            WHERE d.event_id IS NULL OR d.delivered IN (0, 2)
            ORDER BY e.id ASC
        """, (self.destination,)).fetchall()
        db.close()
        logger.debug(f"Found {len(rows)} unprocessed events for destination '{self.destination}'")
        return rows

    def format_event_jsonl(self, event):
        """
        Format event as JSONL (raw event data only).

        Args:
            event: Event row from database

        Returns:
            JSON string (single line)
        """
        return json.dumps({
            "event_id": event["id"],
            "event_type": event["type"],
            "event_payload": json.loads(event["payload"]),
            "event_timestamp": event["created_at"]
        })

    def mark_delivered(self, event_id, success):
        """
        Update delivery status in database.

        Args:
            event_id: Event ID
            success: True if delivered successfully, False if failed
        """
        db = self._get_db()
        try:
            if success:
                logger.debug(f"Marking event {event_id} as successfully delivered to '{self.destination}'")
                db.execute("""
                    INSERT OR REPLACE INTO deliveries
                    (event_id, destination, delivered, delivered_at)
                    VALUES (?, ?, 1, ?)
                """, (event_id, self.destination, int(time.time())))
            else:
                logger.debug(f"Marking event {event_id} as failed for '{self.destination}'")
                db.execute("""
                    INSERT OR REPLACE INTO deliveries
                    (event_id, destination, delivered, delivered_at)
                    VALUES (?, ?, 2, NULL)
                """, (event_id, self.destination))
            db.commit()
        finally:
            db.close()

    @abstractmethod
    def deliver(self, event):
        """
        Deliver event to destination.

        Args:
            event: Event row from database

        Raises:
            Exception: If delivery fails
        """
        pass

    def run(self):
        """Main worker loop - polls database and pushes events."""
        logger.info(f"Event pusher running. Destination: {self.destination}")

        while not self.shutdown_requested:
            events = self.get_unprocessed_events()

            if events:
                logger.info(f"Processing {len(events)} event(s)...")

                for event in events:
                    if self.shutdown_requested:
                        break

                    try:
                        # Deliver to destination (implemented by subclass)
                        logger.debug(f"Delivering event {event['id']} ({event['type']}) to '{self.destination}'")
                        self.deliver(event)

                        # Mark as successfully delivered
                        self.mark_delivered(event['id'], success=True)

                        logger.info(f"✓ Delivered event {event['id']} ({event['type']})")

                    except Exception as e:
                        # Mark as failed for retry
                        self.mark_delivered(event['id'], success=False)
                        logger.warning(f"✗ Failed to deliver event {event['id']}: {e}")
            else:
                # No events to process, sleep
                time.sleep(0.5)

        logger.info("Event pusher stopped.")


class FileEventPusher(BaseEventPusher):
    """Event pusher that writes JSONL to a local file."""

    def __init__(self, db_path, output_path, destination=None):
        """
        Initialize file event pusher.

        Args:
            db_path: Path to SQLite database
            output_path: Path to output file (e.g., events.log)
            destination: Destination name for tracking (defaults to output_path)
        """
        if destination is None:
            destination = output_path
        super().__init__(db_path, destination)
        self.output_path = output_path
        logger.info(f"Output: {self.output_path}")

    def deliver(self, event):
        """
        Write event as JSONL to file.

        Args:
            event: Event row from database

        Raises:
            Exception: If file write fails
        """
        jsonl_line = self.format_event_jsonl(event)
        logger.debug(f"Writing event {event['id']} to {self.output_path}")

        with open(self.output_path, 'a') as f:
            f.write(jsonl_line + '\n')


class CloudEventPusher(BaseEventPusher):
    """Event pusher that sends events to score-cloud API via HTTP."""

    def __init__(self, db_path, cloud_api_url, device_id="device-001", destination=None):
        """
        Initialize cloud event pusher.

        Args:
            db_path: Path to SQLite database
            cloud_api_url: Base URL of the score-cloud API (e.g., "http://localhost:8001")
            device_id: Device identifier for tracking
            destination: Destination name for tracking (defaults to "cloud:{cloud_api_url}")
        """
        if destination is None:
            destination = f"cloud:{cloud_api_url}"
        super().__init__(db_path, destination)
        self.cloud_api_url = cloud_api_url.rstrip('/')
        self.device_id = device_id
        self.session_id = f"session-{int(time.time())}"
        logger.info(f"Cloud API URL: {self.cloud_api_url}")
        logger.info(f"Device ID: {self.device_id}")

    def deliver(self, event):
        """
        Send event to cloud API via HTTP POST.

        Args:
            event: Event row from database

        Raises:
            Exception: If HTTP request fails
        """
        import requests
        from datetime import datetime, timezone

        game_id = event["game_id"]
        if not game_id:
            # Skip events without game_id (clock mode events)
            logger.debug(f"Skipping event {event['id']} - no game_id (clock mode)")
            return

        # Format event for cloud API
        cloud_event = {
            "event_id": f"{self.device_id}-{event['id']}",
            "seq": event["id"],
            "type": event["type"],
            "ts_local": datetime.fromtimestamp(event["created_at"], timezone.utc).isoformat(),
            "payload": json.loads(event["payload"])
        }

        # Send to cloud API
        url = f"{self.cloud_api_url}/v1/games/{game_id}/events"
        payload = {
            "device_id": self.device_id,
            "session_id": self.session_id,
            "events": [cloud_event]
        }

        logger.debug(f"Sending event {event['id']} to {url}")
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()

        response_data = response.json()
        logger.debug(f"Cloud API response: {response_data}")


# For backwards compatibility
EventPusher = FileEventPusher

