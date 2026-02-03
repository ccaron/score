"""Database utilities for score-app."""

import logging
import sqlite3

logger = logging.getLogger("score.db")


def get_db(db_path: str):
    """Get database connection with Row factory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str):
    """Initialize database with required tables.

    Creates events and deliveries tables if they don't exist.
    Handles migrations for schema changes.
    """
    logger.info("Initializing database...")
    db = get_db(db_path)

    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            game_id TEXT,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            event_id INTEGER NOT NULL,
            destination TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0,
            delivered_at INTEGER,
            PRIMARY KEY (event_id, destination),
            FOREIGN KEY (event_id) REFERENCES events(id)
        )
    """)

    # Check if game_id column exists (for migration)
    cursor = db.execute("PRAGMA table_info(events)")
    columns = [col[1] for col in cursor.fetchall()]
    if "game_id" not in columns:
        logger.info("Migrating database: adding game_id column to events")
        db.execute("ALTER TABLE events ADD COLUMN game_id TEXT")

    # Log event count
    count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if count == 0:
        logger.info("New database - no initial events needed for clock mode")
    else:
        logger.info(f"Database initialized with {count} existing events")

    db.commit()
    db.close()
