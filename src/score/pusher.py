import json
import logging
import signal
import sqlite3
import time
from abc import ABC, abstractmethod
from typing import Optional

# Set up logger for this module
logger = logging.getLogger("score.pusher")


# ---------- Error Categories ----------

class DeliveryError(Exception):
    """Base class for delivery errors."""
    pass


class TransientError(DeliveryError):
    """Temporary error that should be retried (network issues, timeouts, 5xx errors)."""
    pass


class PermanentError(DeliveryError):
    """Permanent error that should not be retried (bad data, 4xx errors)."""
    pass


class BaseEventPusher(ABC):
    """
    Base class for event pushers.

    Handles common functionality:
    - Database operations (querying events, tracking deliveries)
    - Main run loop with graceful shutdown
    - Retry logic with exponential backoff
    - Error categorization (transient vs permanent)

    Subclasses implement deliver() for specific destination types.
    """

    # Retry configuration
    MAX_RETRIES = 10  # Maximum number of retry attempts
    INITIAL_BACKOFF = 1  # Initial backoff in seconds
    BACKOFF_MULTIPLIER = 2  # Exponential backoff multiplier
    MAX_BACKOFF = 3600  # Maximum backoff in seconds (1 hour)

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

        # Ensure database schema includes retry tracking
        self._ensure_schema()

    def _ensure_schema(self):
        """Ensure deliveries table has retry tracking columns."""
        db = self._get_db()
        try:
            # Check if retry columns exist
            cursor = db.execute("PRAGMA table_info(deliveries)")
            columns = [col[1] for col in cursor.fetchall()]

            if "retry_count" not in columns:
                logger.info("Adding retry_count column to deliveries table")
                db.execute("ALTER TABLE deliveries ADD COLUMN retry_count INTEGER DEFAULT 0")

            if "last_attempt_at" not in columns:
                logger.info("Adding last_attempt_at column to deliveries table")
                db.execute("ALTER TABLE deliveries ADD COLUMN last_attempt_at INTEGER")

            db.commit()
        finally:
            db.close()

    def _signal_handler(self, _signum, _frame):
        """Handle shutdown signals gracefully."""
        logger.info("Shutdown signal received. Finishing current batch...")
        self.shutdown_requested = True

    def _get_db(self):
        """
        Get database connection with timeout and error handling.

        Returns:
            sqlite3.Connection: Database connection

        Raises:
            Exception: If database connection fails
        """
        try:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as e:
            logger.error(f"Database connection failed: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected database error: {e}")
            raise

    def _calculate_backoff(self, retry_count):
        """
        Calculate exponential backoff delay.

        Args:
            retry_count: Number of previous retry attempts

        Returns:
            Backoff delay in seconds
        """
        backoff = self.INITIAL_BACKOFF * (self.BACKOFF_MULTIPLIER ** retry_count)
        return min(backoff, self.MAX_BACKOFF)

    def get_unprocessed_events(self):
        """
        Get events that haven't been delivered or should be retried.

        Implements exponential backoff by filtering out events that are:
        - Still in their backoff period (recently failed)
        - Have exceeded max retry attempts

        Returns:
            List of event rows that are ready to process
        """
        db = self._get_db()
        current_time = int(time.time())

        # Query events that:
        # 1. Have no delivery record (never attempted)
        # 2. Failed but are ready for retry (past backoff period and under max retries)
        rows = db.execute("""
            SELECT e.*,
                   COALESCE(d.retry_count, 0) as retry_count,
                   d.last_attempt_at,
                   d.delivered
            FROM events e
            LEFT JOIN deliveries d ON e.id = d.event_id AND d.destination = ?
            WHERE
                -- Never attempted
                (d.event_id IS NULL)
                OR
                -- Failed but eligible for retry
                (d.delivered = 2
                 AND COALESCE(d.retry_count, 0) < ?
                 AND (d.last_attempt_at IS NULL OR ? - d.last_attempt_at >= ?))
            ORDER BY e.id ASC
        """, (
            self.destination,
            self.MAX_RETRIES,
            current_time,
            0  # We'll calculate proper backoff in Python for each event
        )).fetchall()

        # Filter events based on exponential backoff
        ready_events = []
        for row in rows:
            retry_count = row['retry_count'] or 0
            last_attempt = row['last_attempt_at']

            # If never attempted, include it
            if last_attempt is None:
                ready_events.append(row)
                continue

            # Calculate required backoff based on retry count
            required_backoff = self._calculate_backoff(retry_count)
            time_since_attempt = current_time - last_attempt

            if time_since_attempt >= required_backoff:
                ready_events.append(row)
                if retry_count > 0:
                    logger.debug(
                        f"Event {row['id']} ready for retry {retry_count + 1}/{self.MAX_RETRIES} "
                        f"after {time_since_attempt}s (required: {required_backoff}s)"
                    )

        if len(ready_events) < len(rows):
            backing_off = len(rows) - len(ready_events)
            logger.debug(f"Found {len(ready_events)} ready events, {backing_off} still in backoff period")
        else:
            logger.debug(f"Found {len(ready_events)} unprocessed events for destination '{self.destination}'")

        db.close()
        return ready_events

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

    def mark_delivered(self, event_id, success, retry_count=0, error_msg=None):
        """
        Update delivery status in database.

        Args:
            event_id: Event ID
            success: True if delivered successfully, False if failed
            retry_count: Number of retry attempts so far
            error_msg: Optional error message for logging
        """
        db = self._get_db()
        try:
            current_time = int(time.time())

            if success:
                logger.debug(f"Marking event {event_id} as successfully delivered to '{self.destination}'")
                db.execute("""
                    INSERT OR REPLACE INTO deliveries
                    (event_id, destination, delivered, delivered_at, retry_count, last_attempt_at)
                    VALUES (?, ?, 1, ?, ?, ?)
                """, (event_id, self.destination, current_time, retry_count, current_time))
            else:
                new_retry_count = retry_count + 1
                if new_retry_count >= self.MAX_RETRIES:
                    logger.warning(
                        f"Event {event_id} exceeded max retries ({self.MAX_RETRIES}), "
                        f"giving up. Last error: {error_msg}"
                    )
                else:
                    next_backoff = self._calculate_backoff(new_retry_count)
                    logger.debug(
                        f"Marking event {event_id} as failed (attempt {new_retry_count}/{self.MAX_RETRIES}). "
                        f"Next retry in {next_backoff}s. Error: {error_msg}"
                    )

                db.execute("""
                    INSERT OR REPLACE INTO deliveries
                    (event_id, destination, delivered, retry_count, last_attempt_at)
                    VALUES (?, ?, 2, ?, ?)
                """, (event_id, self.destination, new_retry_count, current_time))

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
        """Main worker loop - polls database and pushes events with exponential backoff."""
        logger.info(f"Event pusher running. Destination: {self.destination}")
        logger.info(f"Retry config: max={self.MAX_RETRIES}, initial_backoff={self.INITIAL_BACKOFF}s, "
                   f"multiplier={self.BACKOFF_MULTIPLIER}, max_backoff={self.MAX_BACKOFF}s")

        while not self.shutdown_requested:
            try:
                events = self.get_unprocessed_events()
            except Exception as e:
                logger.error(f"Failed to query events from database: {e}")
                time.sleep(5)  # Wait before retrying database query
                continue

            if events:
                logger.info(f"Processing {len(events)} event(s)...")

                for event in events:
                    if self.shutdown_requested:
                        break

                    event_id = event['id']
                    retry_count = event['retry_count'] or 0

                    try:
                        # Deliver to destination (implemented by subclass)
                        logger.debug(f"Delivering event {event_id} ({event['type']}) to '{self.destination}' "
                                   f"(attempt {retry_count + 1})")
                        self.deliver(event)

                        # Mark as successfully delivered
                        self.mark_delivered(event_id, success=True, retry_count=retry_count)

                        if retry_count > 0:
                            logger.info(f"✓ Delivered event {event_id} ({event['type']}) after {retry_count} retries")
                        else:
                            logger.info(f"✓ Delivered event {event_id} ({event['type']})")

                    except PermanentError as e:
                        # Permanent errors should not be retried - mark as failed permanently
                        error_msg = str(e)
                        logger.error(
                            f"✗ Permanent error for event {event_id}: {error_msg}. "
                            f"Will not retry."
                        )
                        # Mark with max retries to prevent further attempts
                        self.mark_delivered(event_id, success=False,
                                          retry_count=self.MAX_RETRIES,
                                          error_msg=error_msg)

                    except TransientError as e:
                        # Transient errors should be retried with backoff
                        error_msg = str(e)
                        next_backoff = self._calculate_backoff(retry_count + 1)
                        logger.warning(
                            f"✗ Transient error for event {event_id}: {error_msg}. "
                            f"Will retry in {next_backoff}s (attempt {retry_count + 1}/{self.MAX_RETRIES})"
                        )
                        self.mark_delivered(event_id, success=False,
                                          retry_count=retry_count,
                                          error_msg=error_msg)

                    except Exception as e:
                        # Unknown errors - treat as transient and retry
                        error_msg = f"{type(e).__name__}: {e}"
                        next_backoff = self._calculate_backoff(retry_count + 1)
                        logger.warning(
                            f"✗ Unexpected error for event {event_id}: {error_msg}. "
                            f"Will retry in {next_backoff}s (attempt {retry_count + 1}/{self.MAX_RETRIES})"
                        )
                        self.mark_delivered(event_id, success=False,
                                          retry_count=retry_count,
                                          error_msg=error_msg)
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
        Write event as JSONL to file with proper error handling.

        Args:
            event: Event row from database

        Raises:
            TransientError: For temporary file issues (permissions, disk full)
            PermanentError: For permanent issues (bad path)
        """
        jsonl_line = self.format_event_jsonl(event)
        logger.debug(f"Writing event {event['id']} to {self.output_path}")

        try:
            with open(self.output_path, 'a') as f:
                f.write(jsonl_line + '\n')

        except FileNotFoundError as e:
            # File or directory doesn't exist - permanent error
            raise PermanentError(f"File not found: {e}")

        except PermissionError as e:
            # Permission denied - could be transient (file locked) or permanent
            raise TransientError(f"Permission denied: {e}")

        except OSError as e:
            # Disk full, I/O errors - transient
            raise TransientError(f"OS error: {e}")

        except Exception as e:
            # Other errors - let base class handle
            raise


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
        Send event to cloud API via HTTP POST with proper error categorization.

        Args:
            event: Event row from database

        Raises:
            TransientError: For network issues, timeouts, or 5xx errors (should retry)
            PermanentError: For bad data or client errors (should not retry)
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

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()

            response_data = response.json()
            logger.debug(f"Cloud API response: {response_data}")

        except requests.exceptions.Timeout as e:
            raise TransientError(f"Request timeout: {e}")

        except requests.exceptions.ConnectionError as e:
            raise TransientError(f"Connection error: {e}")

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else None

            # Categorize HTTP errors
            if status_code:
                if status_code >= 500:
                    # 5xx errors are server errors - should retry
                    raise TransientError(f"Server error {status_code}: {e}")
                elif status_code == 408:
                    # Request Timeout - should retry
                    raise TransientError(f"Request timeout {status_code}: {e}")
                elif status_code == 429:
                    # Too Many Requests - should retry with backoff
                    raise TransientError(f"Rate limited {status_code}: {e}")
                elif status_code >= 400:
                    # 4xx errors (except 408, 429) are client errors - don't retry
                    raise PermanentError(f"Client error {status_code}: {e}")
                else:
                    # Other errors - treat as transient
                    raise TransientError(f"HTTP error {status_code}: {e}")
            else:
                raise TransientError(f"HTTP error: {e}")

        except requests.exceptions.RequestException as e:
            # Other request exceptions - treat as transient
            raise TransientError(f"Request failed: {e}")

        except json.JSONDecodeError as e:
            # Invalid JSON response - could be transient server issue
            raise TransientError(f"Invalid JSON response: {e}")

        except Exception as e:
            # Unexpected errors - let the base class handle them
            raise


# For backwards compatibility
EventPusher = FileEventPusher

